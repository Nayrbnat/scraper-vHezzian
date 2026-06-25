"""Sync onboarded users from the hezzian app DB into scraper_news.user_profiles (Phase 3.6).

The hezzian tables (``users``, ``user_profiles``) are foreign/app-owned, so the read uses a static
``text()`` SELECT with NO interpolated values (zero injection surface) — the deliberate exception
to the ORM-only seam rule, for reading a second database. The upsert into our own
``user_profiles`` uses the ORM + ``pg_insert`` (same pattern as ``embeddings_jobs.seed_owner``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import UserProfile

_SOURCE_QUERY = text(
    """
    SELECT u.clerk_user_id, u.email, p.interests, p.investor_type, p.experience_level,
           p.risk_tolerance, p.primary_objective, p.time_horizon
    FROM user_profiles p
    JOIN users u ON u.id = p.user_id
    WHERE u.deleted_at IS NULL AND p.onboarding_completed = true
    """
)


@dataclass(frozen=True, slots=True)
class HezzianUserRow:
    clerk_user_id: str
    email: str
    interests: dict | str | None
    investor_type: str | None
    experience_level: str | None
    risk_tolerance: str | None
    primary_objective: str | None
    time_horizon: str | None


def _as_dict(interests: dict | str | None) -> dict:
    if isinstance(interests, dict):
        return interests
    if isinstance(interests, str) and interests.strip():
        try:
            parsed = json.loads(interests)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def map_to_profile(row: HezzianUserRow) -> dict:
    """Project a hezzian (users x user_profiles) row into scraper_news.user_profiles fields."""
    interests = _as_dict(row.interests)
    portfolio = _str_list(interests.get("watch_tickers"))
    sectors = _str_list(interests.get("sectors")) + _str_list(interests.get("asset_classes"))
    regions = _str_list(interests.get("regions"))
    focus_parts = [
        row.investor_type,
        f"{row.risk_tolerance} risk" if row.risk_tolerance else None,
        row.primary_objective,
        row.time_horizon,
        *regions,
    ]
    focus = "; ".join(p for p in focus_parts if p) or None
    return {
        "user_id": row.clerk_user_id,
        "email": row.email,
        "portfolio": portfolio,
        "sectors": sectors,
        "focus": focus,
    }


async def fetch_onboarded_users(
    hezzian_session_factory: async_sessionmaker[AsyncSession],
) -> list[HezzianUserRow]:
    """Read onboarded, non-deleted users from the hezzian app DB."""
    async with hezzian_session_factory() as session:
        rows = (await session.execute(_SOURCE_QUERY)).all()
    return [
        HezzianUserRow(
            clerk_user_id=r[0],
            email=r[1],
            interests=r[2],
            investor_type=r[3],
            experience_level=r[4],
            risk_tolerance=r[5],
            primary_objective=r[6],
            time_horizon=r[7],
        )
        for r in rows
    ]


async def sync_users(
    *,
    hezzian_session_factory: async_sessionmaker[AsyncSession],
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Pull onboarded hezzian users and UPSERT into scraper_news.user_profiles. Returns count."""
    rows = await fetch_onboarded_users(hezzian_session_factory)
    if not rows:
        return 0
    now = datetime.now(UTC)
    async with session_factory() as session:
        for row in rows:
            stmt = pg_insert(UserProfile).values(updated_at=now, **map_to_profile(row))
            stmt = stmt.on_conflict_do_update(
                index_elements=[UserProfile.user_id],
                set_={
                    "email": stmt.excluded.email,
                    "portfolio": stmt.excluded.portfolio,
                    "sectors": stmt.excluded.sectors,
                    "focus": stmt.excluded.focus,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
        await session.commit()
    return len(rows)


def _asyncpg(url: str) -> str:
    """Ensure the asyncpg dialect and strip libpq-only query params (sslmode/channel_binding)."""
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url.split("?", 1)[0]


def run_sync_sync(scraper_url: str, hezzian_url: str) -> int:
    """Sync bridge for the CLI: build both engines, run sync_users, dispose both."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    async def _run() -> int:
        scraper_engine = make_engine(scraper_url)
        hezzian_engine = make_engine(_asyncpg(hezzian_url))
        try:
            return await sync_users(
                hezzian_session_factory=make_sessionmaker(hezzian_engine),
                session_factory=make_sessionmaker(scraper_engine),
            )
        finally:
            # Dispose both even if one raises (a failed dispose mustn't leak the other engine).
            await asyncio.gather(
                hezzian_engine.dispose(), scraper_engine.dispose(), return_exceptions=True
            )

    return asyncio.run(_run())
