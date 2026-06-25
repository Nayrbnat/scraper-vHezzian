"""@db: embed_articles / embed_profiles / score_users / seed_owner pipeline jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import (
    Article,
    UserArticleRelevance,
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


@pytest.mark.db
async def test_score_users_ranks_per_user_and_isolates(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import score_users

    now = datetime.now(UTC)
    ai_vec = [1.0, 0.0, 0.0] + [0.0] * 1533
    oil_vec = [0.0, 1.0, 0.0] + [0.0] * 1533
    await _add_article(
        session_factory, id_="a" * 64, title="AI", content="x", fetched_at=now, embedding=ai_vec
    )
    await _add_article(
        session_factory, id_="o" * 64, title="OIL", content="x", fetched_at=now, embedding=oil_vec
    )
    async with session_factory() as s:
        s.add(
            UserProfileVector(user_id="ai_user", embedding=ai_vec, source_hash="h", updated_at=now)
        )
        s.add(
            UserProfileVector(
                user_id="oil_user", embedding=oil_vec, source_hash="h", updated_at=now
            )
        )
        await s.commit()

    n = await score_users(session_factory=session_factory, window_days=30, top_k=1)
    assert n == 2  # one top row per user

    rows = (await db_session.execute(select(UserArticleRelevance))).scalars().all()
    by_user = {r.user_id: r.article_id for r in rows}
    assert by_user["ai_user"] == "a" * 64  # AI user's top match is the AI article
    assert by_user["oil_user"] == "o" * 64  # isolation: oil user gets the oil article


@pytest.mark.db
async def test_score_users_respects_window(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import score_users

    now = datetime.now(UTC)
    ai_vec = [1.0, 0.0, 0.0] + [0.0] * 1533
    await _add_article(
        session_factory,
        id_="old" + "a" * 61,
        title="AI",
        content="x",
        fetched_at=now - timedelta(days=99),
        embedding=ai_vec,
    )
    async with session_factory() as s:
        s.add(UserProfileVector(user_id="u", embedding=ai_vec, source_hash="h", updated_at=now))
        await s.commit()

    n = await score_users(session_factory=session_factory, window_days=30, top_k=10)
    assert n == 0  # the only article is older than the 30-day window


@pytest.mark.db
async def test_score_users_replaces_stale_rows(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import score_users

    now = datetime.now(UTC)
    ai_vec = [1.0, 0.0, 0.0] + [0.0] * 1533
    # In-window article — will be scored
    await _add_article(
        session_factory, id_="a" * 64, title="AI", content="x", fetched_at=now, embedding=ai_vec
    )
    # Out-of-window article — satisfies the FK for the stale relevance row but won't re-appear
    await _add_article(
        session_factory,
        id_="z" * 64,
        title="stale",
        content="x",
        fetched_at=now - timedelta(days=99),
        embedding=ai_vec,
    )
    async with session_factory() as s:
        s.add(UserProfileVector(user_id="u", embedding=ai_vec, source_hash="h", updated_at=now))
        # A stale score for an article outside the scoring window
        s.add(UserArticleRelevance(user_id="u", article_id="z" * 64, score=0.01, computed_at=now))
        await s.commit()

    n = await score_users(session_factory=session_factory, window_days=30, top_k=10)
    assert n == 1  # only the in-window "aaa..." article is scored
    rows = (await db_session.execute(select(UserArticleRelevance))).scalars().all()
    # The stale "zzz..." row is gone; only the current top-K ("aaa...") remains
    assert [r.article_id for r in rows] == ["a" * 64]
