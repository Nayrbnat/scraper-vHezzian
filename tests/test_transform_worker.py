"""Tests for scrapeforge.worker.transform_worker (W6 — idempotent transform stage).

All DB-touching tests are marked ``@pytest.mark.db`` and require a live pgvector
Postgres instance (see conftest.py for how DATABASE_URL is resolved).

Run the ``@db`` subset with::

    DATABASE_URL='postgresql+asyncpg://scrapeforge:scrapeforge@localhost:55432/scrapeforge_test' \\
        .venv/Scripts/python -m pytest -q -m db tests/test_transform_worker.py

Design
------
- A ``session_factory`` is derived from the same ``_db_url`` session-scoped fixture
  that ``db_session`` uses, so the transform worker and the test assertions both hit
  the same Postgres instance (and start clean after each test truncation).
- ``InMemoryObjectStore`` seeds raw HTML at the pointer's ``object_key``.
- ``InMemoryMessageQueue`` stands in for the Redis-backed RESULTS queue.
- Jobs are seeded via ``create_job`` before each test, matching the contract that the
  transform worker is the SOLE owner of Job-status transitions.
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.models import Job as JobRow
from scrapeforge.core.db.repositories import create_job
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.objectstore.memory import InMemoryObjectStore
from scrapeforge.core.queue.memory import InMemoryMessageQueue
from scrapeforge.core.storage.base import url_id
from scrapeforge.worker.messages import ResultPointer

# ---------------------------------------------------------------------------
# Generic HTML fixture: matches PublicScraper selectors + passes response_is_valid
# ---------------------------------------------------------------------------

# The PublicScraper content selector is:
#   'div.entry-content, article, div.post-content, div.content'
# We use <article> which is in the chain and put 500+ chars of content inside.
_VALID_HTML = """<!DOCTYPE html>
<html>
<head><title>Test Article</title></head>
<body>
  <h1>The Great Article Title</h1>
  <article>
    This is a long article body that is definitely more than five hundred characters long.
    We need it to be long enough to pass the response_is_valid floor check which
    requires the extracted content text to be at least 500 characters.  Adding more
    words here to make sure we comfortably exceed that threshold without any anti-bot
    challenge signatures anywhere in this HTML.  The article continues here with
    additional filler text to ensure the character count is well above five hundred.
    More content to pad out the article so validators are satisfied with the length.
    And even more padding here to be absolutely certain we meet the minimum threshold.
    This is the end of the article body which is now very definitely over 500 characters.
  </article>
</body>
</html>"""

# Short/soft-blocked HTML that fails response_is_valid (too short, no matching content).
_BLOCK_HTML = "<!DOCTYPE html><html><head></head><body><p>Short.</p></body></html>"

# Challenge-signature HTML (Cloudflare marker).
_CHALLENGE_HTML = """<!DOCTYPE html>
<html>
<head><title>Just a moment...</title></head>
<body><p>Please wait while Cloudflare checks your browser.</p></body>
</html>"""

_TEST_URL = "https://example-news.com/article/the-great-article"
_TEST_DOMAIN = "example-news.com"
_TEST_BUCKET = "public"
_TEST_OBJECT_KEY = f"raw/{_TEST_BUCKET}/{url_id(_TEST_URL)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_job_id() -> str:
    return str(uuid.uuid4())


def _make_pointer(
    job_id: str,
    *,
    status: str = "success",
    url: str = _TEST_URL,
    domain: str = _TEST_DOMAIN,
    bucket: str = _TEST_BUCKET,
    object_key: str = _TEST_OBJECT_KEY,
) -> ResultPointer:
    return ResultPointer(
        job_id=job_id,
        object_key=object_key,
        url=url,
        url_id=url_id(url),
        domain=domain,
        bucket=bucket,
        status=status,
        fetched_at=datetime.now(UTC).isoformat(),
    )


def _fake_settings() -> object:
    return types.SimpleNamespace(
        RESULTS_QUEUE="results",
        QUEUE_MAX_RETRIES=2,
    )


# ---------------------------------------------------------------------------
# Fixture: session_factory bound to the same DB as db_session
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` bound to the test DB URL.

    Derived from the session-scoped ``_db_url`` fixture so writes from
    ``handle_result_pointer`` and reads from ``db_session`` both target the
    same Postgres instance.
    """
    engine = create_async_engine(_db_url, echo=False)
    return make_sessionmaker(engine)


# ---------------------------------------------------------------------------
# 1. Success path — article persisted, job marked done
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_success_persists_article_and_marks_job_done(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: raw HTML → extracted article row + job status='done'."""
    from scrapeforge.worker.transform_worker import handle_result_pointer

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    await store.put(_TEST_OBJECT_KEY, _VALID_HTML.encode("utf-8"), "text/html")

    pointer = _make_pointer(job_id)
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)

    # --- Article row assertions ------------------------------------------
    expected_id = url_id(_TEST_URL)
    row = await db_session.get(ArticleRow, expected_id)

    assert row is not None, "Expected an article row in the DB after handle_result_pointer"
    assert row.id == expected_id
    assert row.url == _TEST_URL
    # raw_key must equal the object_key so the claim-check pointer is durable.
    assert row.raw_key == _TEST_OBJECT_KEY
    # Content must be non-empty (extracted by parsers.extract via <article> selector).
    assert row.content, "Article content must be non-empty"
    # bucket stored in meta
    assert row.meta.get("bucket") == _TEST_BUCKET

    # --- Job row assertions ----------------------------------------------
    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "done"
    assert job.result_count == 1
    assert job.finished_at is not None
    assert job.started_at is not None


# ---------------------------------------------------------------------------
# 2. Idempotency — same pointer processed twice → ONE article row; job still done
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_idempotency_same_pointer_twice(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Processing the same pointer twice produces exactly one article row."""
    from sqlalchemy import func, select

    from scrapeforge.worker.transform_worker import handle_result_pointer

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    await store.put(_TEST_OBJECT_KEY, _VALID_HTML.encode("utf-8"), "text/html")

    pointer = _make_pointer(job_id)

    # First invocation.
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)
    # Second invocation — must be idempotent (UPSERT, not duplicate insert).
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)

    expected_id = url_id(_TEST_URL)
    count_stmt = select(func.count()).where(ArticleRow.id == expected_id)
    count = (await db_session.execute(count_stmt)).scalar_one()
    assert count == 1, f"Expected exactly one article row; found {count}"

    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "done"
    # result_count must be 1 (set absolutely, NOT incremented to 2 on second run).
    assert job.result_count == 1, (
        f"result_count should be 1 after idempotent re-delivery; got {job.result_count}"
    )


# ---------------------------------------------------------------------------
# 3. Non-success pointer (e.g. 'challenge') — job → error, no article row
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_non_success_pointer_marks_job_error_no_article(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When pointer.status != 'success', job is marked error; no article row written."""
    from sqlalchemy import select

    from scrapeforge.worker.transform_worker import handle_result_pointer

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    # Even if we seed raw data, the pointer status short-circuits before reading it.
    await store.put(_TEST_OBJECT_KEY, _VALID_HTML.encode("utf-8"), "text/html")

    pointer = _make_pointer(job_id, status="challenge")
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)

    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "error"
    assert job.result_count == 0
    assert job.finished_at is not None
    assert "challenge" in (job.error or "")

    # No article row.
    rows = (await db_session.execute(select(ArticleRow))).scalars().all()
    assert len(rows) == 0, f"Expected no article rows; found {len(rows)}"


# ---------------------------------------------------------------------------
# 4. Missing raw object — job → error, no article row
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_missing_raw_object_marks_job_error(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When object_key is absent from the store, job is marked error (raw payload missing)."""
    from sqlalchemy import select

    from scrapeforge.worker.transform_worker import handle_result_pointer

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    # Do NOT seed anything in the store.

    pointer = _make_pointer(job_id)
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)

    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "error"
    assert "raw payload missing" in (job.error or "")
    assert job.finished_at is not None

    rows = (await db_session.execute(select(ArticleRow))).scalars().all()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# 5. Soft-block raw HTML — job → error, no article row
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_soft_block_raw_marks_job_error(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTML that fails response_is_valid → job marked error, no article row."""
    from sqlalchemy import select

    from scrapeforge.worker.transform_worker import handle_result_pointer

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    await store.put(_TEST_OBJECT_KEY, _BLOCK_HTML.encode("utf-8"), "text/html")

    pointer = _make_pointer(job_id)
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)

    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "error"
    assert job.result_count == 0
    assert job.finished_at is not None

    rows = (await db_session.execute(select(ArticleRow))).scalars().all()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# 6. Challenge-signature HTML also fails — same outcome as soft block
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_challenge_signature_html_marks_job_error(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTML with a Cloudflare 'just a moment' signature fails response_is_valid."""
    from sqlalchemy import select

    from scrapeforge.worker.transform_worker import handle_result_pointer

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    await store.put(_TEST_OBJECT_KEY, _CHALLENGE_HTML.encode("utf-8"), "text/html")

    pointer = _make_pointer(job_id)
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)

    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "error"
    assert job.finished_at is not None

    rows = (await db_session.execute(select(ArticleRow))).scalars().all()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# 7. run_transform_worker drains RESULTS_QUEUE with one pointer → article + job done
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_run_transform_worker_drains_queue(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """run_transform_worker drains one pointer from RESULTS_QUEUE; article + job persisted."""
    from scrapeforge.worker.transform_worker import run_transform_worker

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    await store.put(_TEST_OBJECT_KEY, _VALID_HTML.encode("utf-8"), "text/html")

    queue = InMemoryMessageQueue()
    pointer = _make_pointer(job_id)
    await queue.publish("results", dict(pointer))

    settings = _fake_settings()
    await run_transform_worker(
        queue=queue,
        store=store,
        session_factory=session_factory,
        settings=settings,
    )

    # Queue must be empty after drain.
    assert await queue.size("results") == 0

    # Article row persisted.
    row = await db_session.get(ArticleRow, url_id(_TEST_URL))
    assert row is not None

    # Job done.
    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "done"


# ---------------------------------------------------------------------------
# 8. _selectors_for — routes via registry; unknown domain falls back to PublicScraper
# ---------------------------------------------------------------------------


def test_selectors_for_unknown_domain_returns_public_selectors() -> None:
    """An unregistered domain falls back to PublicScraper generic selectors."""
    from scrapeforge.worker.transform_worker import _selectors_for

    selectors = _selectors_for("totally-unknown-domain-xyz.com")
    assert "content" in selectors
    assert "title" in selectors
    # PublicScraper uses 'h1' somewhere in the title selector chain.
    assert "h1" in selectors["title"]


def test_selectors_for_returns_dict() -> None:
    """_selectors_for always returns a dict (never None)."""
    from scrapeforge.worker.transform_worker import _selectors_for

    result = _selectors_for("example.com")
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 9. Job is marked 'running' before any processing
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_job_marked_running_before_processing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The transform worker marks the job 'running' (started_at set) before anything else."""
    from scrapeforge.worker.transform_worker import handle_result_pointer

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    store = InMemoryObjectStore()
    await store.put(_TEST_OBJECT_KEY, _VALID_HTML.encode("utf-8"), "text/html")

    pointer = _make_pointer(job_id)
    await handle_result_pointer(pointer, store=store, session_factory=session_factory)

    # After processing, started_at must be set (proves it was marked running).
    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.started_at is not None


# ---------------------------------------------------------------------------
# 10. Resilience — unhandled store error → requeue then DLQ; Job stays 'running'
# ---------------------------------------------------------------------------


class _BrokenObjectStore(InMemoryObjectStore):
    """Test double that always raises ``RuntimeError`` from ``get``."""

    async def get(self, key: str) -> bytes:
        raise RuntimeError("simulated infrastructure failure")


@pytest.mark.db
async def test_unhandled_store_error_deadletters_and_leaves_job_running(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-ObjectNotFound error from store.get() propagates out of handle_result_pointer.

    The consume_once retry logic in MessageQueue catches the exception, requeues
    the message up to max_retries times, then dead-letters it.  The Job is left
    in status='running' (the terminal update never executes), which is the correct
    observable state for an infrastructure failure — it is NOT silently swallowed
    and the message is NOT lost.

    With max_retries=2 and InMemoryMessageQueue:
    - call 1: reserve (attempts=1 ≤ 2) → handler raises → requeue
    - call 2: reserve (attempts=2 ≤ 2) → handler raises → requeue
    - call 3: reserve (attempts=3 > 2) → handler raises → dead_letter
    - call 4 (from drain loop): queue empty → consume_once returns False → loop exits
    """
    from scrapeforge.worker.transform_worker import run_transform_worker

    job_id = _new_job_id()
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source=_TEST_DOMAIN, params={})

    # _BrokenObjectStore seeds nothing but always raises RuntimeError on get().
    store = _BrokenObjectStore()

    queue = InMemoryMessageQueue()
    pointer = _make_pointer(job_id)
    await queue.publish("results", dict(pointer))

    settings = _fake_settings()  # QUEUE_MAX_RETRIES=2
    await run_transform_worker(
        queue=queue,
        store=store,
        session_factory=session_factory,
        settings=settings,
    )

    # --- Queue assertions -----------------------------------------------
    # After 3 handler attempts (1 + 2 retries) the message must be dead-lettered,
    # not left in the waiting queue.
    assert await queue.size("results") == 0, "Waiting queue must be empty after drain"
    dead = queue.dead_letters("results")
    assert len(dead) == 1, f"Expected exactly 1 dead-lettered message; got {len(dead)}"

    # --- Job assertions -------------------------------------------------
    # Job was marked 'running' on the first attempt; the RuntimeError occurs
    # before any terminal status update, so the job is left non-terminal.
    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "running", (
        f"Job should be left 'running' (non-terminal) after an unhandled store error; "
        f"got '{job.status}'"
    )
    assert job.finished_at is None, "finished_at must not be set when the job is non-terminal"
