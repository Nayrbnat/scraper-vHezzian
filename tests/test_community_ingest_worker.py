"""Tests for the community-ingest worker (Phase 1 lean).

Reuses the in-memory fakes for queue + object store; ``@db`` tests use the ephemeral
pgvector instance via the ``session_factory`` fixture (mirrors test_transform_worker).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.repositories import create_job
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.objectstore.memory import InMemoryObjectStore
from scrapeforge.core.storage.base import url_id
from scrapeforge.worker.messages import IngestMessage, raw_object_key


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_db_url, echo=False)
    return make_sessionmaker(engine)


class _FakeScraper:
    """Returns canned scrape_publication results without network I/O."""

    def __init__(self, results: list[ScrapeResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []

    async def scrape_publication(self, target, limit=50, sort="new"):  # noqa: ARG002
        self.calls.append((target, limit))
        return list(self._results)


def _success(url: str, title: str) -> ScrapeResult:
    return ScrapeResult(
        status="success",
        driver_used="curl_cffi",
        article=Article(
            url=url,
            title=title,
            content="Body text here, long enough to be a real article body.",
            author="A. Writer",
            raw_html=f"<div>{title}</div>",
            metadata={"bucket": "community", "source_domain": "chipstrat.com"},
        ),
    )


def _paywalled() -> ScrapeResult:
    return ScrapeResult(status="error", driver_used="curl_cffi", article=None, error="paywalled")


def _msg(job_id: str) -> IngestMessage:
    return IngestMessage(
        job_id=job_id, platform="substack", target="www.chipstrat.com", bucket="community", limit=5
    )


def test_resolve_scraper_returns_substack_instance() -> None:
    from scrapeforge.scrapers.community.substack import SubstackScraper
    from scrapeforge.worker.community_ingest_worker import _resolve_scraper

    scraper = _resolve_scraper("substack")
    assert isinstance(scraper, SubstackScraper)


def test_resolve_scraper_raises_for_unknown_platform() -> None:
    from scrapeforge.worker.community_ingest_worker import _resolve_scraper

    with pytest.raises(ValueError, match="nope"):
        _resolve_scraper("nope")


@pytest.mark.db
async def test_drain_loop_processes_queue_and_marks_job_done(
    db_session: AsyncSession, session_factory, monkeypatch
) -> None:
    import types

    from scrapeforge.core.db.models import Job as JobRow
    from scrapeforge.core.queue.memory import InMemoryMessageQueue
    from scrapeforge.worker import community_ingest_worker
    from scrapeforge.worker.community_ingest_worker import run_community_ingest_worker

    job_id = uuid.uuid4().hex
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source="www.chipstrat.com", params={})

    url_a = "https://www.chipstrat.com/p/drain-a"
    fake_scraper = _FakeScraper([_success(url_a, "DrainAlpha")])
    monkeypatch.setattr(community_ingest_worker, "_resolve_scraper", lambda _platform: fake_scraper)

    queue = InMemoryMessageQueue()
    store = InMemoryObjectStore()
    settings = types.SimpleNamespace(INGEST_QUEUE="ingest", QUEUE_MAX_RETRIES=2)

    await queue.publish("ingest", dict(_msg(job_id)))

    assert await queue.size("ingest") == 1

    await run_community_ingest_worker(
        queue=queue, store=store, session_factory=session_factory, settings=settings
    )

    assert await queue.size("ingest") == 0
    job = await db_session.get(JobRow, job_id)
    assert job is not None
    assert job.status == "done"
    assert job.result_count == 1


@pytest.mark.db
async def test_persists_success_articles_and_skips_paywalled(
    db_session: AsyncSession, session_factory
) -> None:
    from scrapeforge.core.db.models import Article as ArticleRow
    from scrapeforge.core.db.models import Job as JobRow
    from scrapeforge.worker.community_ingest_worker import handle_ingest_job

    job_id = uuid.uuid4().hex
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source="www.chipstrat.com", params={})

    url_a = "https://www.chipstrat.com/p/a"
    url_b = "https://www.chipstrat.com/p/b"
    scraper = _FakeScraper([_success(url_a, "Alpha"), _paywalled(), _success(url_b, "Beta")])
    store = InMemoryObjectStore()

    persisted = await handle_ingest_job(
        _msg(job_id), store=store, session_factory=session_factory, scraper=scraper
    )

    assert persisted == 2
    # Both raw payloads archived under the deterministic community key.
    assert await store.exists(raw_object_key("community", url_id(url_a)))
    assert await store.exists(raw_object_key("community", url_id(url_b)))
    # Both rows persisted WITH parsed fields (title/author) — no CSS re-extraction.
    row_a = await db_session.get(ArticleRow, url_id(url_a))
    assert row_a is not None and row_a.title == "Alpha" and row_a.author == "A. Writer"
    assert row_a.bucket == "community"
    # Paywalled post is absent.
    paywall_id = url_id("https://www.chipstrat.com/p/paywalled")
    assert await db_session.get(ArticleRow, paywall_id) is None
    # Job done with the right count.
    job = await db_session.get(JobRow, job_id)
    assert job.status == "done" and job.result_count == 2 and job.finished_at is not None


@pytest.mark.db
async def test_rerun_produces_no_duplicate_rows(db_session: AsyncSession, session_factory) -> None:
    from sqlalchemy import func, select

    from scrapeforge.core.db.models import Article as ArticleRow
    from scrapeforge.worker.community_ingest_worker import handle_ingest_job

    url_a = "https://www.chipstrat.com/p/a"
    results = [_success(url_a, "Alpha")]

    for _ in range(2):
        job_id = uuid.uuid4().hex
        async with session_factory() as s:
            await create_job(s, job_id=job_id, source="www.chipstrat.com", params={})
        await handle_ingest_job(
            _msg(job_id),
            store=InMemoryObjectStore(),
            session_factory=session_factory,
            scraper=_FakeScraper(results),
        )

    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 1  # idempotent UPSERT — no duplicate


@pytest.mark.db
async def test_scrape_failure_marks_job_error_and_reraises(
    db_session: AsyncSession, session_factory
) -> None:
    from scrapeforge.core.db.models import Job as JobRow
    from scrapeforge.worker.community_ingest_worker import handle_ingest_job

    class _Boom:
        async def scrape_publication(self, target, limit=50, sort="new"):
            raise RuntimeError("publication down")

    job_id = uuid.uuid4().hex
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source="www.chipstrat.com", params={})

    with pytest.raises(RuntimeError, match="publication down"):
        await handle_ingest_job(
            _msg(job_id),
            store=InMemoryObjectStore(),
            session_factory=session_factory,
            scraper=_Boom(),
        )

    job = await db_session.get(JobRow, job_id)
    assert job.status == "error" and job.error and "publication down" in job.error
