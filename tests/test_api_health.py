"""Tests for GET /health and GET /ready endpoints (W7 serving API).

TDD order:
  RED  — these tests fail because scrapeforge/api/ does not exist yet.
  GREEN — implement api/ to pass them.

``/health`` requires no auth and no DB; ``/ready`` requires a live DB connection
(``@pytest.mark.db``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scrapeforge.config.settings import Settings
from scrapeforge.core.queue.memory import InMemoryMessageQueue

# ---------------------------------------------------------------------------
# Shared test settings (no DB needed for /health)
# ---------------------------------------------------------------------------

_TEST_STATE_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA="  # 44 chars


def _test_settings(**overrides) -> Settings:
    """Return a Settings object suitable for testing (no .env read required)."""
    kwargs = {
        "STATE_STORE_KEY": _TEST_STATE_KEY,
        "API_KEYS": "testkey",
        "API_RATE_LIMIT_PER_MIN": 120,
        "DATABASE_URL": "postgresql+asyncpg://scrapeforge:scrapeforge@localhost:55432/scrapeforge_test",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# /health — no DB, no auth
# ---------------------------------------------------------------------------


def test_health_ok_no_auth() -> None:
    """GET /health returns 200 {"status": "ok"} without any API key."""
    from scrapeforge.api.app import create_app

    settings = _test_settings()
    app = create_app(settings=settings, queue=InMemoryMessageQueue())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_sets_request_id_header() -> None:
    """The request-id middleware must inject X-Request-ID into every response."""
    from scrapeforge.api.app import create_app

    settings = _test_settings()
    app = create_app(settings=settings, queue=InMemoryMessageQueue())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert "x-request-id" in resp.headers
    # Must be a non-empty value
    assert resp.headers["x-request-id"]


# ---------------------------------------------------------------------------
# /ready — needs a live DB  (@pytest.mark.db)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_ready_ok_with_db(_db_url: str) -> None:
    """GET /ready returns 200 {"status": "ready"} when DB is reachable."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    settings = _test_settings(DATABASE_URL=_db_url)
    engine = make_engine(_db_url)
    session_factory = make_sessionmaker(engine)
    queue = InMemoryMessageQueue()

    app = create_app(settings=settings, session_factory=session_factory, queue=queue)
    with TestClient(app) as client:
        resp = client.get("/ready")

    await engine.dispose()

    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_ready_503_when_db_unreachable() -> None:
    """GET /ready returns 503 when the DB session raises an error."""
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    # Point at a DB that doesn't exist / is not reachable
    bad_url = "postgresql+asyncpg://nouser:nopass@localhost:19999/nonexistent"
    settings = _test_settings(DATABASE_URL=bad_url)
    engine = make_engine(bad_url)
    session_factory = make_sessionmaker(engine)
    queue = InMemoryMessageQueue()

    app = create_app(settings=settings, session_factory=session_factory, queue=queue)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/ready")

    assert resp.status_code == 503
