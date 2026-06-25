"""@db: ingest_subreddits scrapes via scrape_subreddit and UPSERTs quality self-posts."""

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


class _Sub:
    """Stands in for RedditSource (only .subreddit is used)."""

    def __init__(self, subreddit: str) -> None:
        self.subreddit = subreddit


def _post(url: str, *, content: str = "Real analysis body.", score: int = 100) -> ScrapeResult:
    return ScrapeResult(
        status="success",
        driver_used="curl_cffi",
        article=Article(
            url=url,
            title="t",
            content=content,
            metadata={"bucket": "community", "source_domain": "reddit.com", "score": score},
        ),
    )


class _FakeReddit:
    def __init__(self, by_sub: dict) -> None:
        self._by = by_sub

    async def scrape_subreddit(self, subreddit, limit=100, sort="hot"):  # noqa: ARG002
        return self._by.get(subreddit, [])


@pytest.mark.db
async def test_persists_only_content_posts_above_min_score(db_session, session_factory) -> None:
    from scrapeforge.pipeline.jobs import ingest_subreddits

    scraper = _FakeReddit(
        {
            "investing": [
                _post("https://r/i/1", content="real analysis", score=100),
                _post("https://r/i/2", content="", score=500),  # link post (no text) -> skipped
            ],
            "stocks": [_post("https://r/s/1", content="dd", score=5)],  # below floor -> skipped
        }
    )
    n = await ingest_subreddits(
        session_factory=session_factory,
        scraper=scraper,
        subreddits=[_Sub("investing"), _Sub("stocks")],
        limit=10,
        min_score=25,
    )
    assert n == 1
    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 1


@pytest.mark.db
async def test_skips_failing_subreddit(db_session, session_factory) -> None:
    from scrapeforge.exceptions import RateLimitError
    from scrapeforge.pipeline.jobs import ingest_subreddits

    class _Flaky:
        async def scrape_subreddit(self, subreddit, limit=100, sort="hot"):  # noqa: ARG002
            if subreddit == "boom":
                raise RateLimitError("HTTP 429")
            return [_post("https://r/ok/1", score=100)]

    n = await ingest_subreddits(
        session_factory=session_factory,
        scraper=_Flaky(),
        subreddits=[_Sub("boom"), _Sub("ok")],
        limit=10,
        min_score=25,
    )
    assert n == 1  # boom 429'd and was skipped; ok still persisted


@pytest.mark.db
async def test_isolates_malformed_payload(db_session, session_factory) -> None:
    """A soft-block returning non-JSON (JSONDecodeError) must NOT abort the batch."""
    import json

    from scrapeforge.pipeline.jobs import ingest_subreddits

    class _Garbled:
        async def scrape_subreddit(self, subreddit, limit=100, sort="hot"):  # noqa: ARG002
            if subreddit == "blocked":
                raise json.JSONDecodeError("Expecting value", "<html>challenge</html>", 0)
            return [_post("https://r/ok/1", score=100)]

    n = await ingest_subreddits(
        session_factory=session_factory,
        scraper=_Garbled(),
        subreddits=[_Sub("blocked"), _Sub("ok")],
        limit=10,
        min_score=25,
    )
    assert n == 1  # 'blocked' soft-blocked + skipped; 'ok' still persisted


@pytest.mark.db
async def test_empty_subreddit_is_skipped(db_session, session_factory) -> None:
    """A subreddit returning no posts (soft-blocked empty page) is skipped, not an error."""
    from scrapeforge.pipeline.jobs import ingest_subreddits

    scraper = _FakeReddit({"empty": [], "investing": [_post("https://r/i/1", score=100)]})
    n = await ingest_subreddits(
        session_factory=session_factory,
        scraper=scraper,
        subreddits=[_Sub("empty"), _Sub("investing")],
        limit=10,
        min_score=25,
    )
    assert n == 1
    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 1


@pytest.mark.db
async def test_dedup_on_rerun(db_session, session_factory) -> None:
    from scrapeforge.pipeline.jobs import ingest_subreddits

    scraper = _FakeReddit({"investing": [_post("https://r/i/1", score=100)]})
    subs = [_Sub("investing")]
    n1 = await ingest_subreddits(
        session_factory=session_factory, scraper=scraper, subreddits=subs, limit=10
    )
    # Re-run with a fresh sink: the UPSERT (sha256(url) PK) guarantees no duplicate ROW. The
    # per-instance _seen_cache doesn't carry across calls — idempotency is at the DB level.
    await ingest_subreddits(
        session_factory=session_factory, scraper=scraper, subreddits=subs, limit=10
    )
    assert n1 == 1
    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 1  # idempotent: one row, not two
