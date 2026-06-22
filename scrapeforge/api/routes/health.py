"""Health check endpoints — no authentication required (SPEC.md §3.22).

Routes:
    GET /health  — liveness probe; returns ``{"status": "ok"}`` unconditionally.
    GET /ready   — readiness probe; runs ``SELECT 1`` via the DB session.
                   Returns ``{"status": "ready"}`` on success or HTTP 503 on error.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from scrapeforge.api.deps import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — always returns 200 if the process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:  # noqa: B008
    """Readiness probe — verifies the database is reachable.

    Raises:
        HTTPException(503): If the database connection fails.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database not ready: {exc}") from exc
    return {"status": "ready"}
