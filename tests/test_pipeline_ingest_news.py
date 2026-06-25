"""@db: ingest_news_feeds fetches each feed via scrape_feed and UPSERTs into Postgres."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article, ScrapeResult


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


class _Feed:
    """Stands in for NewsFeed (only .feed_url and .name are used)."""

    def __init__(self, name: str, feed_url: str) -> None:
        self.name = name
        self.feed_url = feed_url


def _item(url: str) -> ScrapeResult:
    return ScrapeResult(
        status="success",
        driver_used="curl_cffi",
        article=Article(
            url=url,
            title="t",
            content="Real article body.",
            metadata={"bucket": "public", "source_domain": "news.example.com", "via": "rss"},
        ),
    )


class _FakeNews:
    def __init__(self, by_url: dict) -> None:
        self._by = by_url

    async def scrape_feed(self, feed_url, limit=25, min_chars=0):  # noqa: ARG002
        return self._by.get(feed_url, [])


@pytest.mark.db
async def test_ingest_news_persists(db_session, session_factory) -> None:
    from scrapeforge.pipeline.jobs import ingest_news_feeds

    scraper = _FakeNews(
        {
            "https://tc.com/feed/": [_item("https://tc.com/a"), _item("https://tc.com/b")],
            "https://cb.com/feed/": [_item("https://cb.com/a")],
        }
    )
    feeds = [_Feed("TC", "https://tc.com/feed/"), _Feed("CB", "https://cb.com/feed/")]
    n = await ingest_news_feeds(
        session_factory=session_factory, scraper=scraper, feeds=feeds, limit=10
    )
    assert n == 3
    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 3


@pytest.mark.db
async def test_ingest_news_skips_failing_feed(db_session, session_factory) -> None:
    from scrapeforge.pipeline.jobs import ingest_news_feeds

    class _Flaky:
        async def scrape_feed(self, feed_url, limit=25, min_chars=0):  # noqa: ARG002
            if "boom" in feed_url:
                raise ValueError("non-XML challenge body")  # not a ScrapeForgeError
            return [_item("https://ok.com/a")]

    feeds = [_Feed("Boom", "https://boom.com/feed/"), _Feed("OK", "https://ok.com/feed/")]
    n = await ingest_news_feeds(
        session_factory=session_factory, scraper=_Flaky(), feeds=feeds, limit=10
    )
    assert n == 1  # boom feed failed (non-ScrapeForgeError) but was isolated; ok still persisted


@pytest.mark.db
async def test_ingest_news_dedup(db_session, session_factory) -> None:
    from scrapeforge.pipeline.jobs import ingest_news_feeds

    scraper = _FakeNews({"https://tc.com/feed/": [_item("https://tc.com/a")]})
    feeds = [_Feed("TC", "https://tc.com/feed/")]
    n1 = await ingest_news_feeds(
        session_factory=session_factory, scraper=scraper, feeds=feeds, limit=10
    )
    await ingest_news_feeds(session_factory=session_factory, scraper=scraper, feeds=feeds, limit=10)
    assert n1 == 1
    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 1  # idempotent via the sha256(url) UPSERT
