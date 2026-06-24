"""Automatic storage retention: prune old / irrelevant articles to stay under the DB size cap.

Neon's free tier caps storage (~0.5 GB), so the article table must not grow unbounded.
``prune_articles`` deletes oldest-first: anything past the retention window, then scored-but-
irrelevant rows past half the window, then (as a hard safety net) the oldest rows above an
absolute cap. All deletes are SQLAlchemy Core with bound parameters (no raw SQL). Wired into the
daily pipeline so storage self-manages.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article

log = logging.getLogger(__name__)


class RetentionSettings(BaseSettings):
    """Per-module retention config (never core Settings — Invariant #16)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    RETENTION_DAYS: int = Field(default=30)  # keep articles fetched within this window
    RETENTION_MAX_ARTICLES: int = Field(default=5000)  # absolute hard cap (oldest pruned over this)
    RETENTION_RELEVANCE_FLOOR: int = Field(
        default=3
    )  # drop scored-below-this older than half-window


async def prune_articles(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    retention_days: int,
    max_articles: int,
    relevance_floor: int = 0,
    now: datetime | None = None,
) -> int:
    """Delete old / irrelevant articles oldest-first. Returns the number of rows deleted.

    Order of operations:
    1. Age: delete everything fetched before ``now - retention_days``.
    2. Irrelevance (if ``relevance_floor`` > 0): delete scored rows with ``relevance < floor``
       older than half the window — clears low-value content before the full retention period.
    3. Hard cap: if the table still exceeds ``max_articles``, delete the oldest excess.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    deleted = 0
    async with session_factory() as session:
        res = await session.execute(delete(Article).where(Article.fetched_at < cutoff))
        deleted += res.rowcount or 0

        if relevance_floor and relevance_floor > 0:
            half_cutoff = now - timedelta(days=max(1, retention_days // 2))
            res = await session.execute(
                delete(Article).where(
                    Article.relevance.is_not(None),
                    Article.relevance < relevance_floor,
                    Article.fetched_at < half_cutoff,
                )
            )
            deleted += res.rowcount or 0

        total = await session.scalar(select(func.count()).select_from(Article))
        if total and total > max_articles:
            excess = total - max_articles
            oldest_ids = (
                (
                    await session.execute(
                        select(Article.id).order_by(Article.fetched_at.asc()).limit(excess)
                    )
                )
                .scalars()
                .all()
            )
            res = await session.execute(delete(Article).where(Article.id.in_(oldest_ids)))
            deleted += res.rowcount or 0

        await session.commit()

    log.info(
        "prune: deleted %d article(s) (retention=%dd, cap=%d)",
        deleted,
        retention_days,
        max_articles,
    )
    return deleted
