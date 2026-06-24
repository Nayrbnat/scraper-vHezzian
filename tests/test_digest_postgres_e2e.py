"""@db hermetic e2e: seed scored articles -> make_digest(postgres) -> rendered HTML, ranked + floored."""  # noqa: E501

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.digest.models import DigestPreferences, Subscriber


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


@pytest.fixture(autouse=True)
def _wire_env(_db_url, fake_env, monkeypatch):
    """Point Settings().DATABASE_URL at the test DB (fake_env supplies STATE_STORE_KEY)."""
    monkeypatch.setenv("DATABASE_URL", _db_url)


def _sub() -> Subscriber:
    return Subscriber(
        id="dee", name="Dee", email="dee@example.com", preferences=DigestPreferences()
    )


async def _add(session_factory, *, id_, relevance, bullet):
    async with session_factory() as s:
        s.add(
            ArticleRow(
                id=id_,
                url=f"https://e.com/{id_}",
                domain="e.com",
                bucket="community",
                title=f"Art {id_[:3]}",
                content="Body.",
                author=None,
                publish_date=None,
                fetched_at=datetime.now(UTC) - timedelta(hours=1),
                raw_key=None,
                meta={},
                relevance=relevance,
                summary={
                    "bullets": [bullet, "b2"],
                    "reason": "r",
                    "model": "m",
                    "generated_at": "2026-06-24T00:00:00+00:00",
                },
            )
        )
        await s.commit()


@pytest.mark.db
async def test_make_digest_postgres_ranks_and_floors(db_session, session_factory) -> None:
    from scrapeforge.digest.service import make_digest

    await _add(session_factory, id_="a" * 64, relevance=9, bullet="HIGH bullet")
    await _add(session_factory, id_="b" * 64, relevance=6, bullet="MID bullet")
    await _add(session_factory, id_="c" * 64, relevance=3, bullet="LOW bullet")

    _digest, email = await asyncio.to_thread(make_digest, _sub(), "postgres")
    html = email.html
    assert html.index("HIGH bullet") < html.index("MID bullet")  # relevance order
    assert "LOW bullet" not in html  # below the 5/10 floor
    assert "9/10" in html and "6/10" in html


@pytest.mark.db
async def test_make_digest_postgres_empty_state(db_session, session_factory) -> None:
    from scrapeforge.digest.service import make_digest

    await _add(session_factory, id_="a" * 64, relevance=2, bullet="meh")
    digest, email = await asyncio.to_thread(make_digest, _sub(), "postgres")
    assert digest.is_empty
    assert "No updates to show right now." in email.html
