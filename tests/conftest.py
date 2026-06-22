"""Shared pytest fixtures.

Deliberately free of product imports for now — ScrapeForge modules don't exist yet, and importing
them here would break collection. As each module lands, add focused fixtures next to its tests (or
extend this file) per TESTING.md. These generic fixtures are safe to use immediately.

Database fixtures
-----------------
Tests annotated ``@pytest.mark.db`` use the ``db_session`` fixture, which connects to a real
Postgres instance.  The target is controlled by ``DATABASE_URL`` in the environment (falls back to
``Settings().DATABASE_URL``).  If the database is unreachable the whole ``@db`` test group is
skipped with a clear message — no ``pg_ctl`` / pytest-postgresql process-spawning involved.

Session lifecycle:
  1. ``_db_engine`` (session-scoped): build the engine once; install ``vector`` extension and
     ``CREATE TABLE IF NOT EXISTS`` all models; dispose on teardown.
  2. ``db_session`` (function-scoped): begin a SAVEPOINT transaction; yield the session; rollback
     to the SAVEPOINT on teardown so no cross-test state bleeds.

This design is compatible with ``asyncio_mode = "auto"`` (set in ``pyproject.toml``).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Generic fixtures (no product imports)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_states_dir(tmp_path: Path) -> Path:
    """An isolated, throwaway directory standing in for ~/.scrapeforge/states/."""
    d = tmp_path / "states"
    d.mkdir()
    return d


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Populate the env with safe, deterministic test config (never real secrets)."""
    values = {
        # 32+ char base64 Fernet key is required by Settings validation; this is a throwaway.
        "STATE_STORE_KEY": "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA=",
        "LOG_LEVEL": "WARNING",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values


@pytest.fixture
def frozen_clock():
    """Convenience wrapper around freezegun for time-dependent units (RateLimiter, TTLs)."""
    from freezegun import freeze_time

    return freeze_time


@pytest.fixture(autouse=True)
def _no_accidental_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Guard rail: unit tests must not hit the real network. Integration tests opt out via marker.

    This does not block anything by itself (drivers aren't imported here yet); it documents intent
    and gives a single place to wire a socket guard once the HTTP layer exists.
    """
    if "integration" in request.keywords:
        return
    # Placeholder for a future socket-blocking guard (e.g. pytest-socket).
    os.environ.setdefault("SCRAPEFORGE_OFFLINE", "1")


# ---------------------------------------------------------------------------
# Database fixtures (``@pytest.mark.db``)
# ---------------------------------------------------------------------------


def _resolve_database_url() -> str:
    """Resolve the target DATABASE_URL (env var > Settings default)."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        from scrapeforge.config.settings import Settings

        return Settings().DATABASE_URL  # type: ignore[attr-defined]
    except Exception:
        return "postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5432/scrapeforge"


@pytest.fixture(scope="session")
def _db_url() -> str:
    """Session-scoped sync fixture: resolve, probe, and initialise the DB once.

    Reads ``DATABASE_URL`` from the environment (falls back to
    ``Settings().DATABASE_URL``).  Probes the connection synchronously via a
    temporary ``asyncio.run()`` call so the probe runs in its OWN event loop,
    completely separate from pytest-asyncio's per-test loops.  If the DB is
    unreachable, all ``@pytest.mark.db`` tests are skipped.

    On successful connection:
    - Installs the ``vector`` extension (idempotent).
    - Runs ``Base.metadata.create_all`` to create tables (no-op if already present).

    Returns the DSN string so ``db_session`` can create a fresh engine per test
    in the correct per-test event loop (avoiding the cross-loop pitfall).
    """
    import asyncio

    import sqlalchemy
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import create_async_engine

    from scrapeforge.core.db.models import Base

    database_url = _resolve_database_url()

    async def _probe_and_init() -> None:
        engine = create_async_engine(database_url, echo=False)
        try:
            async with engine.begin() as conn:
                await conn.execute(sqlalchemy.text("SELECT 1"))
            async with engine.begin() as conn:
                await conn.execute(sqlalchemy.text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_probe_and_init())
    except (OperationalError, OSError, Exception) as exc:  # noqa: BLE001
        pytest.skip(
            f"DATABASE_URL not reachable; @db tests need Postgres. "
            f"Set DATABASE_URL env var to a running pgvector instance. Error: {exc}"
        )

    return database_url


@pytest_asyncio.fixture
async def db_session(_db_url: str) -> AsyncGenerator[AsyncSession, None]:
    """Function-scoped ``AsyncSession`` with per-test table-truncation isolation.

    Creates a fresh async engine and session per test, running in the
    test's own event loop (no cross-loop sharing with session-scoped fixtures).

    Because our tests call ``await session.commit()`` (testing that persistence
    actually works), a SAVEPOINT-based rollback cannot be used — committing from
    inside the test promotes the SAVEPOINT to a real transaction and the outer
    rollback no longer covers it.

    Instead, we TRUNCATE all application tables in CASCADE order after each test
    so no cross-test state accumulates.  This is slightly slower but is 100%
    reliable for tests that call ``commit()``.

    Usage::

        @pytest.mark.db
        async def test_something(db_session: AsyncSession) -> None:
            ...
    """
    import sqlalchemy
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            yield session
    finally:
        # Teardown: truncate all application tables so the next test starts clean.
        async with engine.begin() as conn:
            await conn.execute(
                sqlalchemy.text("TRUNCATE TABLE articles, jobs, sources RESTART IDENTITY CASCADE")
            )
        await engine.dispose()
