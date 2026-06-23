"""@db: the batch summarizer writes relevance+summary, is idempotent, paces, and skips on error."""

from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.llm.base import SummaryResult
from scrapeforge.core.llm.exceptions import LLMParseError, LLMRateLimitError


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


def _settings(**over):
    base = {
        "SUMMARY_BATCH_SIZE": 10,
        "SUMMARY_INTER_REQUEST_DELAY": 0.0,
        "SUMMARY_PORTFOLIO_LIST": ["Nvidia"],
        "SUMMARY_INTERESTS_LIST": ["hybrid bonding"],
    }
    base.update(over)
    ns = types.SimpleNamespace(
        SUMMARY_BATCH_SIZE=base["SUMMARY_BATCH_SIZE"],
        SUMMARY_INTER_REQUEST_DELAY=base["SUMMARY_INTER_REQUEST_DELAY"],
    )
    ns.portfolio = lambda: base["SUMMARY_PORTFOLIO_LIST"]
    ns.interests = lambda: base["SUMMARY_INTERESTS_LIST"]
    return ns


class _FakeSummarizer:
    def __init__(self, *, raise_on=None, error=None):
        self.calls = []
        self._raise_on = raise_on or set()
        self._error = error

    async def summarize(self, *, title, content, published, portfolio, interests):
        self.calls.append((title, tuple(portfolio), tuple(interests), published))
        if title in self._raise_on:
            raise self._error
        return SummaryResult(
            bullets=["a", "b", "c", "d", "e"],
            relevance=7,
            scores={"relevance": 7, "credibility": 6, "intensity": 5, "personal": 8, "time": 4},
            reason="r",
            model="glm-4.5-flash",
        )


async def _add_article(session_factory, *, id_, title, summary=None):
    async with session_factory() as s:
        s.add(
            ArticleRow(
                id=id_,
                url=f"https://e.com/{id_}",
                domain="e.com",
                bucket="community",
                title=title,
                content="Body.",
                author=None,
                publish_date=None,
                fetched_at=datetime.now(UTC),
                raw_key=None,
                meta={},
                summary=summary,
            )
        )
        await s.commit()


@pytest.mark.db
async def test_summarizes_only_unsummarized(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="New")
    await _add_article(
        session_factory, id_="b" * 64, title="Done", summary={"bullets": ["x"], "model": "m"}
    )

    fake = _FakeSummarizer()
    n = await summarize_pending(
        session_factory=session_factory, summarizer=fake, settings=_settings()
    )
    assert n == 1
    assert fake.calls[0][1] == ("Nvidia",) and fake.calls[0][2] == ("hybrid bonding",)

    row = await db_session.get(ArticleRow, "a" * 64)
    assert row.relevance == 7
    assert row.summary["bullets"] == ["a", "b", "c", "d", "e"]
    assert row.summary["scores"]["personal"] == 8
    assert row.summary["model"] == "glm-4.5-flash"
    assert "generated_at" in row.summary


@pytest.mark.db
async def test_idempotent_rerun_does_nothing(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="New")
    s = _settings()
    assert (
        await summarize_pending(
            session_factory=session_factory, summarizer=_FakeSummarizer(), settings=s
        )
        == 1
    )
    assert (
        await summarize_pending(
            session_factory=session_factory, summarizer=_FakeSummarizer(), settings=s
        )
        == 0
    )


@pytest.mark.db
async def test_parse_error_skips_row_without_aborting(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="Bad")
    await _add_article(session_factory, id_="c" * 64, title="Good")
    fake = _FakeSummarizer(raise_on={"Bad"}, error=LLMParseError("nope"))

    n = await summarize_pending(
        session_factory=session_factory, summarizer=fake, settings=_settings()
    )
    assert n == 1  # only "Good" persisted
    bad = await db_session.get(ArticleRow, "a" * 64)
    good = await db_session.get(ArticleRow, "c" * 64)
    assert bad.summary is None  # left NULL → retried later
    assert good.summary is not None


@pytest.mark.db
async def test_rate_limit_stops_run(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="RateLimited")
    await _add_article(session_factory, id_="c" * 64, title="NeverReached")
    fake = _FakeSummarizer(raise_on={"RateLimited"}, error=LLMRateLimitError("429"))

    n = await summarize_pending(
        session_factory=session_factory, summarizer=fake, settings=_settings()
    )
    assert n == 0
    remaining = (
        (await db_session.execute(select(ArticleRow).where(ArticleRow.summary.is_(None))))
        .scalars()
        .all()
    )
    assert len(remaining) == 2  # run stopped; nothing summarized


@pytest.mark.db
async def test_batch_size_caps_per_run(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    for i in range(3):
        await _add_article(session_factory, id_=str(i) * 64, title=f"A{i}")
    n = await summarize_pending(
        session_factory=session_factory,
        summarizer=_FakeSummarizer(),
        settings=_settings(SUMMARY_BATCH_SIZE=2),
    )
    assert n == 2


@pytest.mark.db
async def test_run_worker_drains_all(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import run_summarize_worker

    for i in range(3):
        await _add_article(session_factory, id_=str(i) * 64, title=f"A{i}")
    await run_summarize_worker(
        session_factory=session_factory,
        summarizer=_FakeSummarizer(),
        settings=_settings(SUMMARY_BATCH_SIZE=2),
    )
    remaining = (
        (await db_session.execute(select(ArticleRow).where(ArticleRow.summary.is_(None))))
        .scalars()
        .all()
    )
    assert remaining == []
