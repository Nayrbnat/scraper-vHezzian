"""Tests for GET /articles and GET /articles/{id} endpoints (W7 serving API).

All tests are marked ``@pytest.mark.db`` because they need a real Postgres row to
assert against.  The ``db_session`` fixture handles table setup + per-test truncation.

TDD order:
  RED  — fail before scrapeforge/api/ exists.
  GREEN — implement routes/articles.py to pass them.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from scrapeforge.config.settings import Settings
from scrapeforge.core.queue.memory import InMemoryMessageQueue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_STATE_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA="
_API_KEY = "testkey"


def _test_settings(**overrides) -> Settings:
    kwargs = {
        "STATE_STORE_KEY": _TEST_STATE_KEY,
        "API_KEYS": _API_KEY,
        "API_RATE_LIMIT_PER_MIN": 120,
        "DATABASE_URL": "postgresql+asyncpg://scrapeforge:scrapeforge@localhost:55432/scrapeforge_test",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[call-arg]


def _url_id(url: str) -> str:
    """sha256 hex digest of url — matches ArticleSink.url_id."""
    return hashlib.sha256(url.encode()).hexdigest()


def _seed_article(session: AsyncSession, **overrides):
    """Return an unsaved Article ORM instance for testing."""
    from scrapeforge.core.db.models import Article

    url = overrides.pop("url", "https://example.com/article-1")
    defaults = {
        "id": _url_id(url),
        "url": url,
        "domain": "example.com",
        "bucket": "public",
        "title": "Test Article",
        "content": "Body text here.",
        "fetched_at": datetime.now(UTC),
        "meta": {"driver_used": "curl_cffi"},
    }
    defaults.update(overrides)
    article = Article(**defaults)
    session.add(article)
    return article


# ---------------------------------------------------------------------------
# Auth guard — no key → 401
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_articles_without_api_key_returns_401(db_session: AsyncSession, _db_url: str) -> None:
    """GET /articles without X-API-Key must return 401."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    settings = _test_settings(DATABASE_URL=_db_url)
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get("/articles")

    await engine.dispose()
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /articles — list, filtering
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_articles_list_returns_seeded_row(db_session: AsyncSession, _db_url: str) -> None:
    """GET /articles with valid key returns 200 and the seeded article."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    # Seed an article using the SAME session the db_session fixture already opened
    _seed_article(db_session, url="https://ft.com/article-99", domain="ft.com", bucket="premium")
    await db_session.commit()

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    settings = _test_settings(DATABASE_URL=_db_url)
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get("/articles", headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    article = next(a for a in body if a["url"] == "https://ft.com/article-99")
    # Verify ArticleOut shape
    assert article["domain"] == "ft.com"
    assert article["bucket"] == "premium"
    assert "id" in article
    assert "title" in article
    assert "content" in article
    assert "fetched_at" in article


@pytest.mark.db
async def test_articles_filter_by_domain(db_session: AsyncSession, _db_url: str) -> None:
    """GET /articles?domain= filters correctly; only matching domain returned."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    _seed_article(db_session, url="https://ft.com/a", domain="ft.com", bucket="premium")
    _seed_article(db_session, url="https://example.com/b", domain="example.com", bucket="public")
    await db_session.commit()

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    settings = _test_settings(DATABASE_URL=_db_url)
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get("/articles?domain=ft.com", headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert resp.status_code == 200
    body = resp.json()
    assert all(a["domain"] == "ft.com" for a in body)
    assert len(body) == 1


@pytest.mark.db
async def test_articles_filter_by_bucket(db_session: AsyncSession, _db_url: str) -> None:
    """GET /articles?bucket= filters by bucket."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    _seed_article(db_session, url="https://ft.com/c", domain="ft.com", bucket="premium")
    _seed_article(db_session, url="https://reddit.com/d", domain="reddit.com", bucket="community")
    await db_session.commit()

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    settings = _test_settings(DATABASE_URL=_db_url)
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get("/articles?bucket=community", headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert resp.status_code == 200
    body = resp.json()
    assert all(a["bucket"] == "community" for a in body)
    assert len(body) == 1


# ---------------------------------------------------------------------------
# GET /articles/{article_id} — single article
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_get_article_by_id_200(db_session: AsyncSession, _db_url: str) -> None:
    """GET /articles/{id} returns the article when it exists."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    url = "https://example.com/single-article"
    _seed_article(db_session, url=url)
    await db_session.commit()
    article_id = _url_id(url)

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    settings = _test_settings(DATABASE_URL=_db_url)
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get(f"/articles/{article_id}", headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == article_id
    assert body["url"] == url


@pytest.mark.db
async def test_get_article_by_id_404(db_session: AsyncSession, _db_url: str) -> None:
    """GET /articles/{id} returns 404 when no row matches."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    settings = _test_settings(DATABASE_URL=_db_url)
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get("/articles/deadbeef" + "0" * 56, headers={"X-API-Key": _API_KEY})

    await engine.dispose()
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth fail-closed security tests (Fix 1)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_auth_empty_api_keys_rejects_even_valid_looking_key(
    db_session: AsyncSession, _db_url: str
) -> None:
    """Fail-closed: if API_KEYS is empty, ALL requests are rejected 401.

    Even a request that sends a plausible X-API-Key header must be denied when
    no keys are configured.  This prevents accidental open-access deployments.
    """
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    # Empty string → api_key_set() returns set() → reject all
    settings = _test_settings(DATABASE_URL=_db_url, API_KEYS="")
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get("/articles", headers={"X-API-Key": "anykeyatall"})

    await engine.dispose()
    assert resp.status_code == 401


@pytest.mark.db
async def test_auth_wrong_key_returns_401(db_session: AsyncSession, _db_url: str) -> None:
    """A request with a key that is not in the configured set must return 401."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    engine = make_engine(_db_url)
    sf = make_sessionmaker(engine)
    settings = _test_settings(DATABASE_URL=_db_url, API_KEYS="correct-key")
    app = create_app(settings=settings, session_factory=sf, queue=InMemoryMessageQueue())

    with TestClient(app) as client:
        resp = client.get("/articles", headers={"X-API-Key": "wrong-key"})

    await engine.dispose()
    assert resp.status_code == 401
