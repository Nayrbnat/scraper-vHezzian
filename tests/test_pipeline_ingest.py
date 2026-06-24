"""@db: ingest_publications scrapes via the injected scraper and UPSERTs into Postgres."""

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


class _FakeSub:
    """Stands in for SubstackSource (only .base is used)."""

    def __init__(self, base: str) -> None:
        self.base = base


class _FakeScraper:
    def __init__(self, by_target):
        self._by = by_target

    async def scrape_publication(self, target, limit=50, sort="new"):  # noqa: ARG002
        out = []
        for url, title in self._by.get(target, []):
            out.append(
                ScrapeResult(
                    status="success",
                    driver_used="curl_cffi",
                    article=Article(
                        url=url,
                        title=title,
                        content="Body.",
                        metadata={"bucket": "community", "source_domain": target},
                    ),
                )
            )
        out.append(
            ScrapeResult(status="error", driver_used="curl_cffi", article=None, error="paywalled")
        )
        return out


@pytest.mark.db
async def test_ingest_publications_upserts(db_session, session_factory) -> None:
    from scrapeforge.pipeline.jobs import ingest_publications

    scraper = _FakeScraper(
        {
            "a.com": [("https://a.com/p/1", "A1")],
            "b.com": [("https://b.com/p/1", "B1"), ("https://b.com/p/2", "B2")],
        }
    )
    sources = [_FakeSub("a.com"), _FakeSub("b.com")]

    n = await ingest_publications(
        session_factory=session_factory, scraper=scraper, sources=sources, limit=5
    )
    assert n == 3  # paywalled error skipped

    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 3
    # idempotent re-run → no dup rows
    await ingest_publications(
        session_factory=session_factory, scraper=scraper, sources=sources, limit=5
    )
    total2 = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total2 == 3
