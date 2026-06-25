"""@db: sync_users upserts mapped hezzian rows into user_profiles (hezzian read mocked)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import UserProfile
from scrapeforge.core.db.session import make_sessionmaker


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


@pytest.mark.db
async def test_sync_users_upserts(db_session, session_factory, monkeypatch) -> None:
    from scrapeforge.pipeline import user_sync
    from scrapeforge.pipeline.user_sync import HezzianUserRow, sync_users

    rows = [
        HezzianUserRow(
            "user_1",
            "one@e.com",
            {"sectors": ["Tech"], "watch_tickers": ["NVDA"]},
            "student",
            "1-3y",
            "low",
            "growth",
            "1-3y",
        ),
        HezzianUserRow(
            "user_2", "two@e.com", {"sectors": ["Energy"]}, "pro", "3y+", "high", "income", "5y+"
        ),
    ]

    async def _fake_fetch(_factory):
        return rows

    monkeypatch.setattr(user_sync, "fetch_onboarded_users", _fake_fetch)

    n = await sync_users(hezzian_session_factory=session_factory, session_factory=session_factory)
    assert n == 2

    got = {r.user_id: r for r in (await db_session.execute(select(UserProfile))).scalars().all()}
    assert got["user_1"].email == "one@e.com"
    assert got["user_1"].portfolio == ["NVDA"]
    assert got["user_1"].sectors == ["Tech"]
    assert got["user_2"].sectors == ["Energy"]

    # Re-run upserts in place (no duplicate, email updated).
    rows[0] = HezzianUserRow(
        "user_1",
        "new@e.com",
        {"sectors": ["Tech"], "watch_tickers": ["AAPL"]},
        "student",
        "1-3y",
        "low",
        "growth",
        "1-3y",
    )
    n2 = await sync_users(hezzian_session_factory=session_factory, session_factory=session_factory)
    assert n2 == 2
    # The upsert committed via a separate session; drop db_session's identity-map cache so the
    # re-read returns the fresh rows rather than the stale objects loaded above.
    db_session.expire_all()
    rows2 = (await db_session.execute(select(UserProfile))).scalars().all()
    assert len([r for r in rows2 if r.user_id in ("user_1", "user_2")]) == 2
    u1 = (
        await db_session.execute(select(UserProfile).where(UserProfile.user_id == "user_1"))
    ).scalar_one()
    assert u1.email == "new@e.com" and u1.portfolio == ["AAPL"]
