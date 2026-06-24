"""Phase-3 multi-user embedding jobs (pure-async; injected Embedder).

Mirrors the summarize worker's shape. This module will hold ``embed_articles`` plus (added in
later tasks) ``embed_profiles``, ``score_users``, and ``seed_owner``. Queries/updates are inlined
here (not added to ``repositories.py``) per the seam rules. No raw SQL.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article, UserArticleRelevance, UserProfile, UserProfileVector
from scrapeforge.core.embeddings.base import Embedder

log = logging.getLogger(__name__)

_ARTICLE_TEXT_CHARS = 2000  # title + leading body fed to the embedder


async def embed_articles(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    batch_size: int,
) -> int:
    """Embed articles WHERE ``embedding IS NULL`` (newest first). Returns rows updated."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Article.id, Article.title, Article.content)
                .where(Article.embedding.is_(None))
                .order_by(Article.fetched_at.desc(), Article.id.desc())
                .limit(batch_size)
            )
        ).all()
    if not rows:
        return 0

    texts = [f"{title}\n\n{(content or '')[:_ARTICLE_TEXT_CHARS]}" for _id, title, content in rows]
    vectors = await embedder.embed(texts)

    updated = 0
    async with session_factory() as session:
        for (article_id, _title, _content), vector in zip(rows, vectors, strict=True):
            await session.execute(
                update(Article).where(Article.id == article_id).values(embedding=vector)
            )
            updated += 1
        await session.commit()
    log.info("embed_articles: embedded %d article(s)", updated)
    return updated


def _profile_text(portfolio: list[str], sectors: list[str], focus: str | None) -> str:
    port = ", ".join(portfolio) or "(none)"
    sect = ", ".join(sectors) or "(none)"
    return (
        f"Investor profile. Portfolio holdings: {port}. "
        f"Sectors of interest: {sect}. Focus: {focus or 'general investing'}."
    )


def _profile_hash(portfolio: list[str], sectors: list[str], focus: str | None) -> str:
    raw = "|".join(portfolio) + "||" + "|".join(sectors) + "||" + (focus or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def embed_profiles(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
) -> int:
    """Embed each user profile whose content hash changed. Returns profiles (re-)embedded."""
    async with session_factory() as session:
        profiles = (
            await session.execute(
                select(
                    UserProfile.user_id,
                    UserProfile.portfolio,
                    UserProfile.sectors,
                    UserProfile.focus,
                )
            )
        ).all()
        existing = dict(
            (
                await session.execute(
                    select(UserProfileVector.user_id, UserProfileVector.source_hash)
                )
            ).all()
        )

    changed = [
        (uid, portfolio or [], sectors or [], focus)
        for uid, portfolio, sectors, focus in profiles
        if _profile_hash(portfolio or [], sectors or [], focus) != existing.get(uid)
    ]
    if not changed:
        return 0

    texts = [_profile_text(p, s, f) for _uid, p, s, f in changed]
    vectors = await embedder.embed(texts)

    now = datetime.now(UTC)
    async with session_factory() as session:
        for (uid, portfolio, sectors, focus), vector in zip(changed, vectors, strict=True):
            stmt = pg_insert(UserProfileVector).values(
                user_id=uid,
                embedding=vector,
                source_hash=_profile_hash(portfolio, sectors, focus),
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[UserProfileVector.user_id],
                set_={
                    "embedding": stmt.excluded.embedding,
                    "source_hash": stmt.excluded.source_hash,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
        await session.commit()
    log.info("embed_profiles: (re-)embedded %d profile(s)", len(changed))
    return len(changed)


async def seed_owner(*, session_factory: async_sessionmaker[AsyncSession], settings) -> None:
    """Upsert a single ``user_id='owner'`` profile from the SUMMARY_* settings (idempotent)."""
    stmt = pg_insert(UserProfile).values(
        user_id="owner",
        portfolio=settings.portfolio(),
        sectors=settings.interests(),
        focus=settings.SUMMARY_FOCUS,
        updated_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UserProfile.user_id],
        set_={
            "portfolio": stmt.excluded.portfolio,
            "sectors": stmt.excluded.sectors,
            "focus": stmt.excluded.focus,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    async with session_factory() as session:
        await session.execute(stmt)
        await session.commit()
    log.info("seed_owner: upserted owner profile")


async def score_users(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    window_days: int,
    top_k: int,
) -> int:
    """Rank recent articles per user by cosine similarity; UPSERT top-K. Returns rows written."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    now = datetime.now(UTC)

    async with session_factory() as session:
        users = (
            await session.execute(select(UserProfileVector.user_id, UserProfileVector.embedding))
        ).all()

    written = 0
    for user_id, uvec in users:
        uvec_list = list(uvec)
        distance = Article.embedding.cosine_distance(uvec_list)
        async with session_factory() as session:
            ranked = (
                await session.execute(
                    select(Article.id, distance.label("dist"))
                    .where(Article.embedding.is_not(None), Article.fetched_at >= cutoff)
                    .order_by(distance)
                    .limit(top_k)
                )
            ).all()
            for article_id, dist in ranked:
                stmt = pg_insert(UserArticleRelevance).values(
                    user_id=user_id,
                    article_id=article_id,
                    score=1.0 - float(dist),
                    computed_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        UserArticleRelevance.user_id,
                        UserArticleRelevance.article_id,
                    ],
                    set_={"score": stmt.excluded.score, "computed_at": stmt.excluded.computed_at},
                )
                await session.execute(stmt)
                written += 1
            await session.commit()
    log.info("score_users: wrote %d (user, article) score(s)", written)
    return written
