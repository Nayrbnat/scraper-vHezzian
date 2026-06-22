"""Tests for POST /jobs, GET /jobs/{id}, GET /jobs endpoints (W7 serving API).

Critical invariant tests:
- Invariant #18: routes/jobs.py must NOT import ScrapeEngine / StealthBridge / driver / worker.
  Verified via AST inspection.
- POST /jobs MUST persist a Job row AND publish exactly one message to the queue.

All tests marked ``@pytest.mark.db`` (need Postgres for the Job table).

TDD order:
  RED  — fail before scrapeforge/api/ exists.
  GREEN — implement routes/jobs.py to pass them.
"""

from __future__ import annotations

import ast
from pathlib import Path

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


def _make_app(db_url: str, queue: InMemoryMessageQueue, **setting_overrides):
    """Build and return a FastAPI test app with an isolated session factory.

    Returns (app, engine).  The engine is the one injected into the app's session
    factory.  After the ``TestClient`` context manager exits, its internal event
    loop is closed; callers MUST call ``await engine.dispose()`` before opening
    a new async connection on the test's own event loop.
    """
    from scrapeforge.api.app import create_app
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    settings = _test_settings(DATABASE_URL=db_url, **setting_overrides)
    engine = make_engine(db_url)
    sf = make_sessionmaker(engine)
    app = create_app(settings=settings, session_factory=sf, queue=queue)
    return app, engine


# ---------------------------------------------------------------------------
# Invariant #18: AST check — routes/jobs.py must not import engine/driver
# ---------------------------------------------------------------------------


def test_invariant_18_no_engine_imports_in_jobs_route() -> None:
    """AST-inspect routes/jobs.py: must import NO engine/driver/worker symbol.

    Forbidden names: ScrapeEngine, StealthBridge, any driver class, any worker handler.
    Only db repositories + queue + schemas are allowed.
    """
    jobs_path = Path(__file__).parent.parent / "scrapeforge" / "api" / "routes" / "jobs.py"
    source = jobs_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {
        "ScrapeEngine",
        "StealthBridge",
        "BaseDriver",
        "CurlCffiDriver",
        "PatchrightDriver",
        "NodriverDriver",
        "PrimpDriver",
        "engine",
        "worker",
    }

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
                if alias.asname:
                    imported_names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # Flag if the module path contains forbidden terms
            for part in module.split("."):
                imported_names.add(part)
            for alias in node.names:
                imported_names.add(alias.name)
                if alias.asname:
                    imported_names.add(alias.asname)

    violations = forbidden & imported_names
    assert not violations, (
        f"routes/jobs.py imports forbidden symbols (Invariant #18): {violations!r}. "
        "The job route MUST only persist + enqueue — no engine, driver, or worker imports."
    )


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_post_jobs_without_api_key_returns_401(
    db_session: AsyncSession, _db_url: str
) -> None:
    """POST /jobs without X-API-Key must return 401."""
    queue = InMemoryMessageQueue()
    app, engine = _make_app(_db_url, queue)

    with TestClient(app) as client:
        resp = client.post("/jobs", json={"source": "example.com"})

    await engine.dispose()
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /jobs — happy path
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_post_jobs_creates_row_and_publishes(db_session: AsyncSession, _db_url: str) -> None:
    """POST /jobs returns 202, persists Job row (status='queued'), publishes one message."""
    from scrapeforge.core.db.repositories import get_job
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    queue = InMemoryMessageQueue()
    app, engine = _make_app(_db_url, queue)

    payload = {"source": "example.com", "urls": ["https://example.com/1"], "bucket": "public"}

    with TestClient(app) as client:
        resp = client.post("/jobs", json=payload, headers={"X-API-Key": _API_KEY})

    # Dispose the engine used by the TestClient (its loop is now closed).
    await engine.dispose()

    assert resp.status_code == 202
    body = resp.json()
    job_id = body["id"]

    # Verify JobOut shape
    assert body["status"] == "queued"
    assert body["source"] == "example.com"
    assert "created_at" in body

    # Verify DB row — use a FRESH engine in this test's event loop
    verify_engine = make_engine(_db_url)
    sf = make_sessionmaker(verify_engine)
    async with sf() as s:
        job = await get_job(s, job_id)
    await verify_engine.dispose()

    assert job is not None
    assert job.status == "queued"
    assert job.source == "example.com"

    # Verify exactly one message published to the job queue
    settings = _test_settings(DATABASE_URL=_db_url)
    queue_size = await queue.size(settings.JOB_QUEUE)
    assert queue_size == 1

    # Reserve and inspect the message
    msg = await queue.reserve(settings.JOB_QUEUE)
    assert msg is not None
    assert msg.payload["job_id"] == job_id
    assert msg.payload["url"] == "example.com"
    assert msg.payload["bucket"] == "public"


@pytest.mark.db
async def test_post_jobs_minimal_body(db_session: AsyncSession, _db_url: str) -> None:
    """POST /jobs with only source field (all optional fields absent) works."""
    queue = InMemoryMessageQueue()
    app, engine = _make_app(_db_url, queue)

    with TestClient(app) as client:
        resp = client.post("/jobs", json={"source": "reddit.com"}, headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert resp.status_code == 202
    body = resp.json()
    assert body["source"] == "reddit.com"
    assert body["status"] == "queued"


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_get_job_200(db_session: AsyncSession, _db_url: str) -> None:
    """GET /jobs/{id} returns 200 + the job after it's created via POST."""
    queue = InMemoryMessageQueue()
    app, engine = _make_app(_db_url, queue)

    with TestClient(app) as client:
        post_resp = client.post("/jobs", json={"source": "ft.com"}, headers={"X-API-Key": _API_KEY})
        job_id = post_resp.json()["id"]
        get_resp = client.get(f"/jobs/{job_id}", headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["id"] == job_id
    assert body["source"] == "ft.com"


@pytest.mark.db
async def test_get_job_404(db_session: AsyncSession, _db_url: str) -> None:
    """GET /jobs/{id} returns 404 for an unknown job id."""
    queue = InMemoryMessageQueue()
    app, engine = _make_app(_db_url, queue)

    with TestClient(app) as client:
        resp = client.get("/jobs/nonexistent-job-id-9999", headers={"X-API-Key": _API_KEY})

    await engine.dispose()
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /jobs — list
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_list_jobs(db_session: AsyncSession, _db_url: str) -> None:
    """GET /jobs returns a list; multiple POSTed jobs appear."""
    queue = InMemoryMessageQueue()
    app, engine = _make_app(_db_url, queue)

    with TestClient(app) as client:
        for source in ("alpha.com", "beta.com"):
            client.post("/jobs", json={"source": source}, headers={"X-API-Key": _API_KEY})
        resp = client.get("/jobs", headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    sources = {j["source"] for j in body}
    assert "alpha.com" in sources
    assert "beta.com" in sources


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_rate_limit_429(db_session: AsyncSession, _db_url: str) -> None:
    """After API_RATE_LIMIT_PER_MIN requests in the same minute, 429 is returned."""
    queue = InMemoryMessageQueue()
    # Low limit: 2 per minute
    app, engine = _make_app(_db_url, queue, API_RATE_LIMIT_PER_MIN=2)

    with TestClient(app) as client:
        r1 = client.get("/articles", headers={"X-API-Key": _API_KEY})
        r2 = client.get("/articles", headers={"X-API-Key": _API_KEY})
        r3 = client.get("/articles", headers={"X-API-Key": _API_KEY})

    await engine.dispose()

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429


@pytest.mark.db
async def test_rate_limit_per_key_isolation(db_session: AsyncSession, _db_url: str) -> None:
    """Rate-limit buckets are per-(key, minute): exhausting key A's budget must NOT block key B.

    With two valid keys and a limit of 2 per minute, exhaust key A's allowance
    (2 requests), then verify key B still receives 200.
    """
    key_a = "key-alpha"
    key_b = "key-beta"
    queue = InMemoryMessageQueue()
    # Configure both keys; low limit to make exhaustion cheap
    app, engine = _make_app(_db_url, queue, API_KEYS=f"{key_a},{key_b}", API_RATE_LIMIT_PER_MIN=2)

    with TestClient(app) as client:
        # Exhaust key A
        ra1 = client.get("/articles", headers={"X-API-Key": key_a})
        ra2 = client.get("/articles", headers={"X-API-Key": key_a})
        ra3 = client.get("/articles", headers={"X-API-Key": key_a})  # should 429

        # Key B has its own separate counter — must still succeed
        rb1 = client.get("/articles", headers={"X-API-Key": key_b})

    await engine.dispose()

    assert ra1.status_code == 200
    assert ra2.status_code == 200
    assert ra3.status_code == 429, "key A should be rate-limited after 2 requests"
    assert rb1.status_code == 200, "key B must not be affected by key A's exhaustion"
