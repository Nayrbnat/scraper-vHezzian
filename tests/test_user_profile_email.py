"""@db: the new email column on user_profiles round-trips and is nullable."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select


@pytest.mark.db
async def test_user_profile_email_roundtrips(db_session) -> None:
    from scrapeforge.core.db.models import UserProfile

    db_session.add(
        UserProfile(
            user_id="u1",
            email="alice@example.com",
            portfolio=["NVDA"],
            sectors=["AI"],
            focus=None,
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    row = (
        await db_session.execute(select(UserProfile).where(UserProfile.user_id == "u1"))
    ).scalar_one()
    assert row.email == "alice@example.com"


@pytest.mark.db
async def test_user_profile_email_nullable(db_session) -> None:
    from scrapeforge.core.db.models import UserProfile

    db_session.add(
        UserProfile(
            user_id="u2", portfolio=[], sectors=[], focus=None, updated_at=datetime.now(UTC)
        )
    )
    await db_session.commit()
    row = (
        await db_session.execute(select(UserProfile).where(UserProfile.user_id == "u2"))
    ).scalar_one()
    assert row.email is None
