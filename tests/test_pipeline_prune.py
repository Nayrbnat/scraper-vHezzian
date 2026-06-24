"""@db: prune_articles deletes old/irrelevant articles oldest-first and enforces the cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


async def _add(session_factory, *, id_, fetched_at, relevance=None) -> None:
    async with session_factory() as s:
        s.add(
            ArticleRow(
                id=id_,
                url=f"https://e.com/{id_}",
                domain="e.com",
                bucket="community",
                title=id_,
                content="Body.",
                fetched_at=fetched_at,
                meta={},
                relevance=relevance,
            )
        )
        await s.commit()


@pytest.mark.db
async def test_prune_deletes_beyond_window_keeps_recent(db_session, session_factory) -> None:
    from scrapeforge.pipeline.retention import prune_articles

    now = datetime.now(UTC)
    await _add(session_factory, id_="old" + "0" * 61, fetched_at=now - timedelta(days=40))
    await _add(session_factory, id_="new" + "0" * 61, fetched_at=now - timedelta(days=1))

    n = await prune_articles(
        session_factory=session_factory, retention_days=30, max_articles=1000, now=now
    )
    assert n == 1
    remaining = (await db_session.execute(select(ArticleRow.id))).scalars().all()
    assert remaining == ["new" + "0" * 61]


@pytest.mark.db
async def test_prune_drops_irrelevant_past_half_window(db_session, session_factory) -> None:
    from scrapeforge.pipeline.retention import prune_articles

    now = datetime.now(UTC)
    # 20 days old (past half of a 30-day window), scored 1 (< floor 3) → pruned
    await _add(
        session_factory, id_="lo" + "0" * 62, fetched_at=now - timedelta(days=20), relevance=1
    )
    # 20 days old but high relevance → kept
    await _add(
        session_factory, id_="hi" + "0" * 62, fetched_at=now - timedelta(days=20), relevance=9
    )

    n = await prune_articles(
        session_factory=session_factory,
        retention_days=30,
        max_articles=1000,
        relevance_floor=3,
        now=now,
    )
    assert n == 1
    remaining = (await db_session.execute(select(ArticleRow.id))).scalars().all()
    assert remaining == ["hi" + "0" * 62]


@pytest.mark.db
async def test_prune_cap_deletes_oldest_excess(db_session, session_factory) -> None:
    from scrapeforge.pipeline.retention import prune_articles

    now = datetime.now(UTC)
    for i in range(5):  # all recent (within window); cap should still trim oldest
        await _add(session_factory, id_=str(i) * 64, fetched_at=now - timedelta(hours=i))

    n = await prune_articles(
        session_factory=session_factory, retention_days=365, max_articles=2, now=now
    )
    assert n == 3  # 5 - 2 cap = 3 oldest removed
    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 2
    # the two NEWEST (smallest hour offset = ids "0..." and "1...") survive
    remaining = set((await db_session.execute(select(ArticleRow.id))).scalars().all())
    assert remaining == {"0" * 64, "1" * 64}
