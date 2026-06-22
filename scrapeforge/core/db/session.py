"""Async SQLAlchemy engine and session-factory helpers (SPEC.md §3.21).

Design decisions
----------------
- **No I/O at import time.** ``make_engine`` and ``make_sessionmaker`` are
  pure factory functions; they build objects but open no connections.  This
  means tests can import the module freely without a live database.
- **Injectable.** Every function accepts an explicit ``database_url`` / engine
  so tests can pass in their ephemeral-PG URL without monkeypatching Settings.
- **Lazy module-level default.** ``get_sessionmaker()`` returns a cached
  ``async_sessionmaker`` built from ``Settings().DATABASE_URL``.  It is only
  called at runtime (API startup / worker startup), never at import time.

Usage
-----
::

    # Application startup (injected once):
    engine = make_engine()                  # reads Settings
    session_factory = make_sessionmaker(engine)

    # Per-request:
    async with session_factory() as session:
        articles = await repo.query_articles(session, domain="ft.com")

    # Tests (explicit URL, no Settings required):
    engine = make_engine("postgresql+asyncpg://user:pw@localhost/testdb")
    session_factory = make_sessionmaker(engine)
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str | None = None) -> AsyncEngine:
    """Return a new ``AsyncEngine`` for *database_url*.

    If *database_url* is ``None``, ``Settings().DATABASE_URL`` is used.
    The engine is configured for production use (pool recycle, pre-ping) but
    works equally well for tests with a single ephemeral connection.

    Args:
        database_url: Optional explicit DSN (overrides ``Settings``).

    Returns:
        A configured ``AsyncEngine``.  No connections are opened yet.
    """
    if database_url is None:
        from scrapeforge.config.settings import Settings

        database_url = Settings().DATABASE_URL

    return create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=3600,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` bound to *engine*.

    Args:
        engine: The ``AsyncEngine`` to bind.

    Returns:
        An ``async_sessionmaker`` with ``expire_on_commit=False`` so ORM
        attributes remain accessible after a ``commit()`` without requiring an
        extra round-trip to the database.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


@lru_cache(maxsize=1)
def _default_engine() -> AsyncEngine:
    """Return the process-wide default engine (built from ``Settings``).

    Cached via ``lru_cache`` so the engine (and its connection pool) is shared
    across calls.  Do **not** call this in tests — pass an explicit engine instead.
    """
    return make_engine()


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return a module-level default ``async_sessionmaker`` (lazy, cached).

    Intended for use by the API and workers where a single session factory
    suffices for the process lifetime.  Tests should call ``make_sessionmaker``
    directly with their own ephemeral engine.

    Returns:
        The default ``async_sessionmaker``.
    """
    return make_sessionmaker(_default_engine())
