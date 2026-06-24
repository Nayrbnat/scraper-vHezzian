"""@db: multi-user contract tables round-trip and cascade on article delete."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from scrapeforge.core.db.models import (
    Article,
    UserArticleRelevance,
    UserProfile,
    UserProfileVector,
)


@pytest.mark.db
async def test_user_profile_roundtrip(db_session) -> None:
    db_session.add(
        UserProfile(
            user_id="owner",
            portfolio=["NVDA", "MSFT"],
            sectors=["AI", "fintech"],
            focus="ai and finance",
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    row = (await db_session.execute(select(UserProfile))).scalar_one()
    assert row.portfolio == ["NVDA", "MSFT"]
    assert row.sectors == ["AI", "fintech"]


@pytest.mark.db
async def test_profile_vector_roundtrip(db_session) -> None:
    db_session.add(
        UserProfileVector(
            user_id="owner",
            embedding=[0.1] * 1536,
            source_hash="abc",
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    row = (await db_session.execute(select(UserProfileVector))).scalar_one()
    assert row.source_hash == "abc"
    assert len(list(row.embedding)) == 1536


@pytest.mark.db
async def test_relevance_cascades_on_article_delete(db_session) -> None:
    art_id = "a" * 64
    db_session.add(
        Article(
            id=art_id,
            url="https://e.com/a",
            domain="e.com",
            bucket="community",
            title="t",
            content="body",
            fetched_at=datetime.now(UTC),
            meta={},
        )
    )
    db_session.add(
        UserArticleRelevance(
            user_id="owner", article_id=art_id, score=0.9, computed_at=datetime.now(UTC)
        )
    )
    await db_session.commit()

    await db_session.execute(delete(Article).where(Article.id == art_id))
    await db_session.commit()

    remaining = (await db_session.execute(select(UserArticleRelevance))).scalars().all()
    assert remaining == []  # FK ON DELETE CASCADE removed the score row
