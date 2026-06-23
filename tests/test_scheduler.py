"""Tests for scrapeforge.worker.scheduler (W8 — periodic job enqueuer).

All tests are marked ``@pytest.mark.db`` and run against the real pgvector container.
Use ``InMemoryMessageQueue`` for the queue layer so no Redis is required.

TDD order:
  RED  — these tests fail first (scheduler module does not exist yet).
  GREEN — implement scheduler.py to pass.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Source
from scrapeforge.core.db.repositories import list_jobs
from scrapeforge.core.queue.memory import InMemoryMessageQueue

# ---------------------------------------------------------------------------
# Shared settings fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_settings():
    """Tiny settings-like namespace; avoids env-var requirements."""
    return types.SimpleNamespace(
        JOB_QUEUE="scrapeforge:jobs",
    )


# ---------------------------------------------------------------------------
# Session-factory builder (bound to the same DB the db_session fixture uses)
# ---------------------------------------------------------------------------


def _make_session_factory(db_url: str):
    """Return an async_sessionmaker bound to *db_url*."""
    engine = create_async_engine(db_url, echo=False)
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Source row helpers
# ---------------------------------------------------------------------------


def _source(
    name: str,
    bucket: str = "public",
    params: dict | None = None,
    enabled: bool = True,
) -> Source:
    return Source(
        name=name,
        bucket=bucket,
        params=params or {},
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# 1. Happy path: two enabled + one disabled source
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_enqueue_due_sources_returns_count(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """Two enabled sources + one disabled -> enqueue_due_sources returns 2."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    # Seed rows directly into the test session so the same transaction is visible.
    db_session.add(_source("ft.com-daily", bucket="premium", params={"url": "https://ft.com"}))
    db_session.add(
        _source("reddit-daily", bucket="community", params={"url": "https://reddit.com"})
    )
    db_session.add(_source("disabled-site", enabled=False))
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    count = await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    assert count == 2


@pytest.mark.db
async def test_enqueue_due_sources_creates_job_rows(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """Two enabled sources -> exactly 2 Job rows with status 'queued' persisted."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    db_session.add(_source("ft.com-daily", bucket="premium", params={"url": "https://ft.com"}))
    db_session.add(
        _source("reddit-daily", bucket="community", params={"url": "https://reddit.com"})
    )
    db_session.add(_source("disabled-site", enabled=False))
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    # Verify DB state via db_session (same DB, different session is fine post-commit).
    jobs = await list_jobs(db_session, limit=10)
    assert len(jobs) == 2
    assert all(j.status == "queued" for j in jobs)


@pytest.mark.db
async def test_enqueue_due_sources_publishes_messages(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """Two enabled sources -> exactly 2 messages published to JOB_QUEUE."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    db_session.add(_source("ft.com-daily", bucket="premium", params={"url": "https://ft.com"}))
    db_session.add(
        _source("reddit-daily", bucket="community", params={"url": "https://reddit.com"})
    )
    db_session.add(_source("disabled-site", enabled=False))
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    assert await queue.size(fake_settings.JOB_QUEUE) == 2


@pytest.mark.db
async def test_disabled_source_is_skipped(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """The disabled source must not produce a Job row or a queue message."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    db_session.add(_source("ft.com-daily", bucket="premium", params={"url": "https://ft.com"}))
    db_session.add(_source("disabled-site", enabled=False, params={"url": "https://disabled.com"}))
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    count = await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    assert count == 1
    assert await queue.size(fake_settings.JOB_QUEUE) == 1

    jobs = await list_jobs(db_session, limit=10)
    assert len(jobs) == 1
    # The one job must be for ft.com-daily, not the disabled source.
    assert jobs[0].source == "ft.com-daily"


# ---------------------------------------------------------------------------
# 2. JobMessage fields: url from params, fallback to source.name
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_message_url_comes_from_params(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """When params contains 'url', the published message carries that URL."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    db_session.add(
        _source("ft.com-daily", bucket="premium", params={"url": "https://ft.com/latest"})
    )
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    msg = await queue.reserve(fake_settings.JOB_QUEUE)
    assert msg is not None
    assert msg.payload["url"] == "https://ft.com/latest"
    assert msg.payload["bucket"] == "premium"
    assert "job_id" in msg.payload
    # The message's job_id must correlate with a persisted Job row (the enqueuer's core
    # contract: the queued row and the published message reference the SAME job).
    from scrapeforge.core.db.repositories import get_job

    job = await get_job(db_session, msg.payload["job_id"])
    assert job is not None and job.status == "queued"


@pytest.mark.db
async def test_message_url_falls_back_to_source_name(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """When params has no 'url', the message url falls back to source.name."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    # No 'url' in params.
    db_session.add(_source("reddit.com-daily", bucket="community", params={"limit": 100}))
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    msg = await queue.reserve(fake_settings.JOB_QUEUE)
    assert msg is not None
    # Falls back to source.name when no 'url' key in params.
    assert msg.payload["url"] == "reddit.com-daily"


@pytest.mark.db
async def test_message_carries_correct_bucket(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """Published message carries the source's bucket value."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    db_session.add(_source("wsj.com-daily", bucket="premium", params={"url": "https://wsj.com"}))
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    msg = await queue.reserve(fake_settings.JOB_QUEUE)
    assert msg is not None
    assert msg.payload["bucket"] == "premium"


# ---------------------------------------------------------------------------
# 3. No enabled sources -> returns 0, no jobs, no messages
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_no_enabled_sources_returns_zero(
    db_session: AsyncSession,
    _db_url: str,
    fake_settings,
) -> None:
    """With no enabled sources, enqueue_due_sources returns 0 and publishes nothing."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    # Only a disabled source.
    db_session.add(_source("only-disabled", enabled=False))
    await db_session.commit()

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    count = await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    assert count == 0
    assert await queue.size(fake_settings.JOB_QUEUE) == 0

    jobs = await list_jobs(db_session, limit=10)
    assert len(jobs) == 0


@pytest.mark.db
async def test_empty_db_returns_zero(
    db_session: AsyncSession,  # noqa: ARG001
    _db_url: str,
    fake_settings,
) -> None:
    """With an empty sources table, enqueue_due_sources returns 0."""
    from scrapeforge.worker.scheduler import enqueue_due_sources

    queue = InMemoryMessageQueue()
    session_factory = _make_session_factory(_db_url)

    count = await enqueue_due_sources(
        session_factory=session_factory,
        queue=queue,
        settings=fake_settings,
    )

    assert count == 0
    assert await queue.size(fake_settings.JOB_QUEUE) == 0


# ---------------------------------------------------------------------------
# 4. SchedulerSettings is importable without live arq/redis
# ---------------------------------------------------------------------------


def test_scheduler_settings_importable() -> None:
    """SchedulerSettings must be importable without requiring a live arq/Redis instance."""
    from scrapeforge.worker.scheduler import SchedulerSettings  # noqa: F401

    assert SchedulerSettings is not None


# ---------------------------------------------------------------------------
# 5. session_factory fixture (mirrors test_transform_worker.py)
# ---------------------------------------------------------------------------

from scrapeforge.core.db.session import make_sessionmaker  # noqa: E402


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` bound to the test DB URL."""
    engine = create_async_engine(_db_url, echo=False)
    return make_sessionmaker(engine)


# ---------------------------------------------------------------------------
# 6. Routing: community platform sources go to INGEST queue
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_community_platform_source_routes_to_ingest_queue(
    db_session,
    session_factory,
) -> None:
    """A Source with params.platform publishes an IngestMessage to INGEST_QUEUE."""
    from scrapeforge.core.db.models import Source
    from scrapeforge.worker.scheduler import enqueue_due_sources

    async with session_factory() as s:
        s.add(
            Source(
                name="substack:newsletter.semianalysis.com",
                bucket="community",
                params={
                    "url": "newsletter.semianalysis.com",
                    "platform": "substack",
                    "limit": 7,
                },
                cron=None,
                enabled=True,
            )
        )
        await s.commit()

    queue = InMemoryMessageQueue()
    settings = types.SimpleNamespace(JOB_QUEUE="jobs", INGEST_QUEUE="ingest")

    n = await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)

    assert n == 1
    assert await queue.size("ingest") == 1
    assert await queue.size("jobs") == 0
    msg = await queue.reserve("ingest")
    assert msg is not None
    assert msg.payload["platform"] == "substack"
    assert msg.payload["target"] == "newsletter.semianalysis.com"
    assert msg.payload["bucket"] == "community"
    assert msg.payload["limit"] == 7
    assert msg.payload["job_id"]


@pytest.mark.db
async def test_non_platform_source_still_routes_to_job_queue(
    db_session,
    session_factory,
) -> None:
    """A Source without params.platform keeps today's JobMessage/JOB_QUEUE behaviour."""
    from scrapeforge.core.db.models import Source
    from scrapeforge.worker.scheduler import enqueue_due_sources

    async with session_factory() as s:
        s.add(
            Source(
                name="ft.com-daily",
                bucket="premium",
                params={"url": "https://ft.com/x"},
                cron=None,
                enabled=True,
            )
        )
        await s.commit()

    queue = InMemoryMessageQueue()
    settings = types.SimpleNamespace(JOB_QUEUE="jobs", INGEST_QUEUE="ingest")

    await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)

    assert await queue.size("jobs") == 1
    assert await queue.size("ingest") == 0
