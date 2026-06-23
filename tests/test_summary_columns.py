"""@db: the new relevance/summary columns round-trip; the prod migration is idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.session import make_sessionmaker


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


@pytest.mark.db
async def test_relevance_and_summary_roundtrip(db_session, session_factory) -> None:
    from scrapeforge.core.db.models import Article as ArticleRow

    async with session_factory() as s:
        s.add(
            ArticleRow(
                id="x" * 64,
                url="https://e.com/a",
                domain="e.com",
                bucket="community",
                title="T",
                content="C",
                author=None,
                publish_date=None,
                fetched_at=datetime.now(UTC),
                raw_key=None,
                meta={},
                relevance=8,
                summary={
                    "bullets": ["a", "b", "c"],
                    "scores": {"relevance": 9},
                    "reason": "r",
                    "model": "glm-4.5-flash",
                    "generated_at": "2026-06-23T00:00:00+00:00",
                },
            )
        )
        await s.commit()

    row = await db_session.get(ArticleRow, "x" * 64)
    assert row.relevance == 8
    assert row.summary["bullets"] == ["a", "b", "c"]
    assert row.summary["scores"]["relevance"] == 9


@pytest.mark.db
async def test_ensure_columns_is_idempotent(_db_url) -> None:
    from scrapeforge.core.db.migrations import ensure_summary_columns

    engine = create_async_engine(_db_url, echo=False)
    await ensure_summary_columns(engine)
    await ensure_summary_columns(engine)  # second run must not raise
    await engine.dispose()
