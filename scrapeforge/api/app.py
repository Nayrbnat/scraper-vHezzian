"""FastAPI application factory for the ScrapeForge serving plane (SPEC.md §3.22).

Usage with uvicorn ``--factory`` flag::

    uvicorn scrapeforge.api.app:create_app --factory --host 0.0.0.0 --port 8000

Design decisions
----------------
- ``create_app`` is a factory so ``app.state`` is injected at test time without
  monkeypatching module-level globals.
- ``session_factory`` and ``queue`` are constructor-injected so tests can supply
  their own ephemeral DB engine and an ``InMemoryMessageQueue``.
- In production, W9 (deployment) will pass a ``RedisQueue`` instance as ``queue``.
  This module does NOT build a Redis client itself (Invariant #18 — no infra coupling
  in the API layer; the caller wires the queue).
- The ``session_factory`` defaults to ``make_sessionmaker(make_engine(settings.DATABASE_URL))``
  only if ``None`` is passed — i.e. only at actual server startup, never at test time.

Middleware added (in order of registration / outermost-first execution):
  1. ``CORSMiddleware``   — allow all origins for now (tightened in production).
  2. Request-ID middleware — injects a UUID into the ``X-Request-ID`` response header.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from scrapeforge.api.routes import articles, health, jobs, search
from scrapeforge.config.settings import Settings
from scrapeforge.core.queue.base import MessageQueue


def create_app(
    *,
    settings: Settings | None = None,
    session_factory=None,
    queue: MessageQueue | None = None,
) -> FastAPI:
    """Build and return the ScrapeForge FastAPI application.

    Args:
        settings:
            ``Settings`` instance to use.  Defaults to ``Settings()`` (reads
            from environment / ``.env``).
        session_factory:
            An ``async_sessionmaker`` bound to the database engine.  If
            ``None``, one is created from ``settings.DATABASE_URL`` lazily.
            Pass an explicit factory in tests to use the test DB.
        queue:
            A ``MessageQueue`` implementation (e.g. ``InMemoryMessageQueue``
            for tests, ``RedisQueue`` in production).  If ``None``, the
            ``get_queue`` dependency will raise ``RuntimeError`` when called —
            production deployments MUST inject a queue via this parameter.

    Returns:
        A configured ``FastAPI`` instance ready to serve.
    """
    resolved_settings = settings or Settings()

    # Resolve the session factory lazily so we never open a DB connection at
    # import time (tests pass an explicit factory; production falls back here).
    if session_factory is None:
        from scrapeforge.core.db.session import make_engine, make_sessionmaker

        session_factory = make_sessionmaker(make_engine(resolved_settings.DATABASE_URL))

    app = FastAPI(
        title="ScrapeForge API",
        description=(
            "Read + enqueue plane for ScrapeForge.  "
            "Serves article data and accepts scrape job submissions."
        ),
        version="0.1.0",
    )

    # ------------------------------------------------------------------
    # app.state — injected dependencies (readable by FastAPI deps)
    # ------------------------------------------------------------------
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.queue = queue
    # Per-key rate-limit counters: (api_key, minute_bucket) -> count
    # Plain dict is safe here — asyncio is single-threaded.
    app.state.rate_limit_counters: dict[tuple[str, int], int] = {}

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next) -> Response:
        """Inject a UUID ``X-Request-ID`` header into every response."""
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(uuid.uuid4())
        return response

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    app.include_router(health.router)
    app.include_router(articles.router)
    app.include_router(jobs.router)
    app.include_router(search.router)

    return app
