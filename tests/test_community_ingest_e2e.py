"""Hermetic end-to-end: seed → scheduler → community-ingest worker → Postgres.

Proves the whole Phase-1 chain with fakes for queue + object store and an ephemeral PG.
The scraper is monkeypatched so no network is touched.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.objectstore.memory import InMemoryObjectStore
from scrapeforge.core.queue.memory import InMemoryMessageQueue


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_db_url, echo=False)
    return make_sessionmaker(engine)


class _FakeScraper:
    def __init__(self, target_to_articles):
        self._map = target_to_articles

    async def scrape_publication(self, target, limit=50, sort="new"):  # noqa: ARG002
        out = []
        for url, title in self._map.get(target, []):
            out.append(
                ScrapeResult(
                    status="success",
                    driver_used="curl_cffi",
                    article=Article(
                        url=url,
                        title=title,
                        content="A sufficiently long article body for the digest.",
                        author="Writer",
                        raw_html=f"<div>{title}</div>",
                        metadata={"bucket": "community", "source_domain": target},
                    ),
                )
            )
        return out


@pytest.mark.db
async def test_seed_to_postgres_end_to_end(
    db_session: AsyncSession, session_factory, monkeypatch
) -> None:
    from scrapeforge.core.db.models import Source
    from scrapeforge.core.db.repositories import query_articles
    from scrapeforge.worker import community_ingest_worker
    from scrapeforge.worker.community_ingest_worker import run_community_ingest_worker
    from scrapeforge.worker.scheduler import enqueue_due_sources

    # 1. Seed two community publication sources.
    async with session_factory() as s:
        s.add_all(
            [
                Source(
                    name="substack:www.chipstrat.com",
                    bucket="community",
                    params={"url": "www.chipstrat.com", "platform": "substack", "limit": 5},
                    cron=None,
                    enabled=True,
                ),
                Source(
                    name="substack:newsletter.semianalysis.com",
                    bucket="community",
                    params={
                        "url": "newsletter.semianalysis.com",
                        "platform": "substack",
                        "limit": 5,
                    },
                    cron=None,
                    enabled=True,
                ),
            ]
        )
        await s.commit()

    # 2. Fake scraper (no network); monkeypatch the worker's platform resolver.
    fake = _FakeScraper(
        {
            "www.chipstrat.com": [("https://www.chipstrat.com/p/a", "Chip Alpha")],
            "newsletter.semianalysis.com": [
                ("https://newsletter.semianalysis.com/p/b", "Semi Beta")
            ],
        }
    )
    monkeypatch.setattr(community_ingest_worker, "_resolve_scraper", lambda platform: fake)

    queue = InMemoryMessageQueue()
    store = InMemoryObjectStore()
    settings = types.SimpleNamespace(JOB_QUEUE="jobs", INGEST_QUEUE="ingest", QUEUE_MAX_RETRIES=2)

    # 3. Scheduler enqueues two IngestMessages.
    n = await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)
    assert n == 2
    assert await queue.size("ingest") == 2

    # 4. Ingest worker drains the queue → articles land in Postgres.
    await run_community_ingest_worker(
        queue=queue, store=store, session_factory=session_factory, settings=settings
    )

    articles = await query_articles(db_session, bucket="community", limit=50)
    titles = {a.title for a in articles}
    assert titles == {"Chip Alpha", "Semi Beta"}
    assert all(a.author == "Writer" for a in articles)
    assert all(a.bucket == "community" for a in articles)

    # 5. Re-run scheduler + worker → no duplicate rows.
    await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)
    await run_community_ingest_worker(
        queue=queue, store=store, session_factory=session_factory, settings=settings
    )
    again = await query_articles(db_session, bucket="community", limit=50)
    assert len(again) == 2  # idempotent — still exactly two rows
