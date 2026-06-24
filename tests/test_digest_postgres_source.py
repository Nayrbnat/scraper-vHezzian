"""@db: load_ranked_articles returns in-window summarized rows, relevance-desc.

Also tests the sync wrapper.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


def _summary(b1: str) -> dict:
    return {
        "bullets": [b1, "b2"],
        "scores": {},
        "reason": "r",
        "model": "m",
        "generated_at": "2026-06-24T00:00:00+00:00",
    }


async def _add(session_factory, *, id_, relevance, summary, hours_ago=1):
    async with session_factory() as s:
        s.add(
            ArticleRow(
                id=id_,
                url=f"https://e.com/{id_}",
                domain="e.com",
                bucket="community",
                title=f"T{id_[:4]}",
                content="Body.",
                author=None,
                publish_date=None,
                fetched_at=datetime.now(UTC) - timedelta(hours=hours_ago),
                raw_key=None,
                meta={},
                relevance=relevance,
                summary=summary,
            )
        )
        await s.commit()


@pytest.mark.db
async def test_load_ranked_articles_window_and_order(db_session, session_factory) -> None:
    import sqlalchemy

    from scrapeforge.digest.postgres_source import load_ranked_articles

    # Ensure a clean table before seeding (conftest truncates AFTER tests, not before).
    await db_session.execute(sqlalchemy.text("DELETE FROM articles"))
    await db_session.commit()

    await _add(session_factory, id_="a" * 64, relevance=6, summary=_summary("low-in"))
    await _add(session_factory, id_="b" * 64, relevance=9, summary=_summary("high-in"))
    await _add(session_factory, id_="c" * 64, relevance=10, summary=_summary("old"), hours_ago=100)
    await _add(session_factory, id_="d" * 64, relevance=8, summary=None)  # unsummarized

    out = await load_ranked_articles(session_factory, window_hours=48, limit=10)
    # only the two in-window summarized rows, relevance-desc:
    assert [a.metadata["relevance"] for a in out] == [9, 6]
    assert out[0].metadata["summary"]["bullets"][0] == "high-in"
    assert out[0].title and out[0].url.startswith("https://")


@pytest.mark.db
def test_sync_wrapper_loads(db_session, _db_url) -> None:
    from scrapeforge.digest.postgres_source import load_ranked_articles_sync

    factory = make_sessionmaker(create_async_engine(_db_url, echo=False))
    asyncio.run(_add(factory, id_="e" * 64, relevance=7, summary=_summary("x")))
    out = load_ranked_articles_sync(_db_url, window_hours=48, limit=10)
    assert any(a.metadata["relevance"] == 7 for a in out)
