"""Per-user digest DB reads (Phase 3.5).

Queries are inlined here (NOT in repositories.py) per the seam rules — exactly as
``digest/postgres_source.py`` does for the single-owner path. Ranking is by the per-user
``user_article_relevance.score`` (cosine in [-1, 1]); the score is used for ORDER/filter only,
not displayed. No raw SQL (SQLi guard, consistent with ``score_users``).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.models import UserArticleRelevance, UserProfile
from scrapeforge.core.models import Article


@dataclass(frozen=True, slots=True)
class ActiveUser:
    """A user we can email. ``name`` is derived from the email local-part — ``user_profiles``
    has no name column in v1, and Clerk's display name isn't mirrored into our table."""

    user_id: str
    email: str
    name: str


async def load_active_users(session_factory: async_sessionmaker[AsyncSession]) -> list[ActiveUser]:
    """All users with a non-NULL email, ordered by user_id for deterministic batches."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(UserProfile.user_id, UserProfile.email)
                .where(UserProfile.email.is_not(None))
                .order_by(UserProfile.user_id)
            )
        ).all()
    return [
        ActiveUser(user_id=uid, email=email, name=email.split("@", 1)[0]) for uid, email in rows
    ]


async def load_user_ranked_articles(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
    *,
    window_hours: int,
    score_floor: float,
    limit: int,
) -> list[Article]:
    """Up to *limit* summarized articles for *user_id*, cosine-desc, within *window_hours* and
    at or above *score_floor*. Each Article carries its shared relevance + summary in metadata."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ArticleRow)
                    .join(UserArticleRelevance, UserArticleRelevance.article_id == ArticleRow.id)
                    .where(
                        UserArticleRelevance.user_id == user_id,
                        ArticleRow.summary.is_not(None),
                        ArticleRow.fetched_at >= cutoff,
                        UserArticleRelevance.score >= score_floor,
                    )
                    .order_by(UserArticleRelevance.score.desc(), ArticleRow.fetched_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        Article(
            url=row.url,
            title=row.title,
            content=row.content,
            author=row.author,
            publish_date=row.publish_date,
            metadata={
                "source_domain": row.domain,
                "bucket": row.bucket,
                "relevance": row.relevance,
                "summary": row.summary,
            },
        )
        for row in rows
    ]


def load_all_sync(
    database_url: str, *, window_hours: int, score_floor: float, limit: int
) -> list[tuple[ActiveUser, list[Article]]]:
    """Sync bridge for the run-once CLI: one engine for the whole batch (mirrors
    ``postgres_source.load_ranked_articles_sync``)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    async def _run() -> list[tuple[ActiveUser, list[Article]]]:
        engine = make_engine(database_url)
        try:
            factory = make_sessionmaker(engine)
            users = await load_active_users(factory)
            out: list[tuple[ActiveUser, list[Article]]] = []
            for user in users:
                articles = await load_user_ranked_articles(
                    factory,
                    user.user_id,
                    window_hours=window_hours,
                    score_floor=score_floor,
                    limit=limit,
                )
                out.append((user, articles))
            return out
        finally:
            await engine.dispose()

    return asyncio.run(_run())
