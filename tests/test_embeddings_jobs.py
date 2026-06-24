"""@db: embed_articles / embed_profiles / score_users / seed_owner pipeline jobs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import (
    Article,
    UserProfile,
    UserProfileVector,
)
from scrapeforge.core.embeddings.base import Embedder


class FakeEmbedder(Embedder):
    """Deterministic 1536-dim embedder: maps a marker substring to a unit axis."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            base = [0.0] * 1536
            if "AI" in t:
                base[0] = 1.0
            elif "OIL" in t:
                base[1] = 1.0
            else:
                base[2] = 1.0
            out.append(base)
        return out


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker:
    return async_sessionmaker(create_async_engine(_db_url, echo=False), expire_on_commit=False)


async def _add_article(session_factory, *, id_, title, content, fetched_at, embedding=None) -> None:
    async with session_factory() as s:
        s.add(
            Article(
                id=id_,
                url=f"https://e.com/{id_}",
                domain="e.com",
                bucket="community",
                title=title,
                content=content,
                fetched_at=fetched_at,
                meta={},
                embedding=embedding,
            )
        )
        await s.commit()


@pytest.mark.db
async def test_embed_articles_fills_null_only(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    now = datetime.now(UTC)
    await _add_article(session_factory, id_="a" * 64, title="AI chips", content="x", fetched_at=now)
    await _add_article(
        session_factory,
        id_="b" * 64,
        title="done",
        content="x",
        fetched_at=now,
        embedding=[0.5, 0.5, 0.5] + [0.0] * 1533,
    )

    n = await embed_articles(
        session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10
    )
    assert n == 1  # only the NULL row embedded
    rows = (await db_session.execute(select(Article).order_by(Article.id))).scalars().all()
    assert list(rows[0].embedding)[0] == 1.0  # "AI" → x-axis, was NULL, now embedded
    assert list(rows[1].embedding)[:3] == [0.5, 0.5, 0.5]  # untouched


@pytest.mark.db
async def test_embed_articles_idempotent(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    now = datetime.now(UTC)
    await _add_article(session_factory, id_="a" * 64, title="AI", content="x", fetched_at=now)
    await embed_articles(session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10)
    n2 = await embed_articles(
        session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10
    )
    assert n2 == 0  # nothing left WHERE embedding IS NULL


async def _add_profile(session_factory, *, user_id, portfolio, sectors, focus=None) -> None:
    async with session_factory() as s:
        s.add(
            UserProfile(
                user_id=user_id,
                portfolio=portfolio,
                sectors=sectors,
                focus=focus,
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()


@pytest.mark.db
async def test_embed_profiles_embeds_then_skips_unchanged(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_profiles

    await _add_profile(session_factory, user_id="u1", portfolio=["NVDA"], sectors=["AI"])
    fake = FakeEmbedder()

    n1 = await embed_profiles(session_factory=session_factory, embedder=fake)
    assert n1 == 1
    vec = (await db_session.execute(select(UserProfileVector))).scalar_one()
    assert vec.user_id == "u1"
    assert vec.source_hash  # non-empty

    n2 = await embed_profiles(session_factory=session_factory, embedder=fake)
    assert n2 == 0  # unchanged profile → skipped


@pytest.mark.db
async def test_embed_profiles_reembeds_on_change(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_profiles

    await _add_profile(session_factory, user_id="u1", portfolio=["NVDA"], sectors=["AI"])
    await embed_profiles(session_factory=session_factory, embedder=FakeEmbedder())

    async with session_factory() as s:
        await s.execute(
            update(UserProfile).where(UserProfile.user_id == "u1").values(sectors=["OIL"])
        )
        await s.commit()

    n = await embed_profiles(session_factory=session_factory, embedder=FakeEmbedder())
    assert n == 1  # hash changed → re-embedded
    vec = (await db_session.execute(select(UserProfileVector))).scalar_one()
    assert list(vec.embedding)[1] == 1.0  # "OIL" → y-axis


@pytest.mark.db
async def test_seed_owner_upserts_from_settings(db_session, session_factory) -> None:
    from scrapeforge.core.llm.settings import SummarizerSettings
    from scrapeforge.pipeline.embeddings_jobs import seed_owner

    settings = SummarizerSettings(
        SUMMARY_PORTFOLIO="NVDA, MSFT", SUMMARY_INTERESTS="AI, fintech", SUMMARY_FOCUS="ai finance"
    )
    await seed_owner(session_factory=session_factory, settings=settings)
    await seed_owner(session_factory=session_factory, settings=settings)  # idempotent

    rows = (await db_session.execute(select(UserProfile))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == "owner"
    assert rows[0].portfolio == ["NVDA", "MSFT"]
    assert rows[0].sectors == ["AI", "fintech"]
    assert rows[0].focus == "ai finance"
