"""FastAPI dependency functions for the ScrapeForge API (SPEC.md §3.22).

All dependencies are injected via ``Depends(...)`` in route handlers.  They
read from ``request.app.state``, which is populated by ``create_app`` in
``api/app.py``.

SRP: this module owns *dependency wiring only*.  Auth logic lives in
``api/auth.py``; session-factory wiring lives in ``core/db/session.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from scrapeforge.config.settings import Settings
from scrapeforge.core.queue.base import MessageQueue


def get_settings(request: Request) -> Settings:
    """Return the ``Settings`` instance stored on ``app.state``."""
    return request.app.state.settings  # type: ignore[no-any-return]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a live ``AsyncSession`` from the app-level session factory.

    The session is managed by the ``async_sessionmaker`` context manager so
    it is committed or rolled back automatically on exit.
    """
    async with request.app.state.session_factory() as session:
        yield session


def get_queue(request: Request) -> MessageQueue:
    """Return the ``MessageQueue`` stored on ``app.state``.

    Raises:
        RuntimeError: If no queue was injected at startup (production must inject one).
    """
    q = request.app.state.queue
    if q is None:
        raise RuntimeError(
            "MessageQueue not configured on app.state.queue. "
            "Pass a queue instance to create_app(queue=...)."
        )
    return q  # type: ignore[return-value]
