"""@db: per-user article loading ranked by user_article_relevance cosine.

Filtered by score floor, time window, and limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.models import UserArticleRelevance, UserProfile
from scrapeforge.core.db.session import make_sessionmaker


@pytest_asyncio.fixture
async def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_db_url, echo=False)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


async def _seed_article(session_factory, *, id_, hours_ago=1, summary=True):
    async with session_factory() as s:
        s.add(
            ArticleRow(
                id=id_,
                url=f"https://e.com/{id_[:6]}",
                domain="e.com",
                bucket="community",
                title=f"Title {id_[:4]}",
                content="Body text.",
                author=None,
                publish_date=None,
                fetched_at=datetime.now(UTC) - timedelta(hours=hours_ago),
                raw_key=None,
                meta={},
                relevance=7,
                summary=(
                    {"bullets": [f"bullet-{id_[:4]}", "b2"], "reason": "r"} if summary else None
                ),
            )
        )
        await s.commit()


async def _seed_score(session_factory, *, user_id, article_id, score):
    async with session_factory() as s:
        s.add(
            UserArticleRelevance(
                user_id=user_id, article_id=article_id, score=score, computed_at=datetime.now(UTC)
            )
        )
        await s.commit()


async def _seed_user(session_factory, *, user_id, email):
    async with session_factory() as s:
        s.add(
            UserProfile(
                user_id=user_id,
                email=email,
                portfolio=[],
                sectors=[],
                focus=None,
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()


@pytest.mark.db
async def test_load_active_users_skips_null_email(db_session, session_factory) -> None:
    from scrapeforge.digest.user_source import load_active_users

    await _seed_user(session_factory, user_id="u1", email="a@e.com")
    async with session_factory() as s:  # a user with no email is skipped
        s.add(
            UserProfile(
                user_id="u2", portfolio=[], sectors=[], focus=None, updated_at=datetime.now(UTC)
            )
        )
        await s.commit()

    users = await load_active_users(session_factory)
    assert [u.user_id for u in users] == ["u1"]
    assert users[0].email == "a@e.com"
    assert users[0].name == "a"  # local-part fallback


@pytest.mark.db
async def test_load_user_ranked_articles_orders_and_filters(db_session, session_factory) -> None:
    from scrapeforge.digest.user_source import load_user_ranked_articles

    await _seed_article(session_factory, id_="a" * 64)
    await _seed_article(session_factory, id_="b" * 64)
    await _seed_article(session_factory, id_="c" * 64, hours_ago=999)  # outside window
    await _seed_article(session_factory, id_="d" * 64)  # below floor
    await _seed_score(session_factory, user_id="u1", article_id="a" * 64, score=0.9)
    await _seed_score(session_factory, user_id="u1", article_id="b" * 64, score=0.4)
    await _seed_score(session_factory, user_id="u1", article_id="c" * 64, score=0.8)
    await _seed_score(session_factory, user_id="u1", article_id="d" * 64, score=-0.5)

    arts = await load_user_ranked_articles(
        session_factory, "u1", window_hours=48, score_floor=0.0, limit=10
    )
    titles = [a.title for a in arts]
    assert titles == ["Title aaaa", "Title bbbb"]  # cosine-desc; c out of window, d below floor
    assert arts[0].metadata["relevance"] == 7
    assert arts[0].metadata["summary"]["bullets"][0] == "bullet-aaaa"


@pytest.mark.db
async def test_load_user_ranked_articles_includes_score_at_floor(
    db_session, session_factory
) -> None:
    from scrapeforge.digest.user_source import load_user_ranked_articles

    # Score exactly equal to the floor must be INCLUDED (>= not >).
    await _seed_article(session_factory, id_="a" * 64)
    await _seed_article(session_factory, id_="b" * 64)
    await _seed_score(session_factory, user_id="u1", article_id="a" * 64, score=0.9)
    await _seed_score(session_factory, user_id="u1", article_id="b" * 64, score=0.4)

    arts = await load_user_ranked_articles(
        session_factory, "u1", window_hours=48, score_floor=0.4, limit=10
    )
    assert "Title bbbb" in [a.title for a in arts]


@pytest.mark.db
async def test_load_user_ranked_articles_respects_limit(db_session, session_factory) -> None:
    from scrapeforge.digest.user_source import load_user_ranked_articles

    await _seed_article(session_factory, id_="a" * 64)
    await _seed_article(session_factory, id_="b" * 64)
    await _seed_score(session_factory, user_id="u1", article_id="a" * 64, score=0.9)
    await _seed_score(session_factory, user_id="u1", article_id="b" * 64, score=0.8)

    arts = await load_user_ranked_articles(
        session_factory, "u1", window_hours=48, score_floor=0.0, limit=1
    )
    assert len(arts) == 1 and arts[0].title == "Title aaaa"


@pytest.mark.db
async def test_load_user_ranked_articles_is_per_user(db_session, session_factory) -> None:
    from scrapeforge.digest.user_source import load_user_ranked_articles

    await _seed_article(session_factory, id_="a" * 64)  # scored for both
    await _seed_article(session_factory, id_="b" * 64)  # scored only for u2
    await _seed_score(session_factory, user_id="u1", article_id="a" * 64, score=0.9)
    await _seed_score(session_factory, user_id="u2", article_id="a" * 64, score=0.1)
    await _seed_score(session_factory, user_id="u2", article_id="b" * 64, score=0.95)

    u1 = await load_user_ranked_articles(
        session_factory, "u1", window_hours=48, score_floor=0.0, limit=10
    )
    assert [a.title for a in u1] == ["Title aaaa"]  # only u1's article, NOT b (u2-only)
