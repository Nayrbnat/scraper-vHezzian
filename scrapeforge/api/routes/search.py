"""Semantic search endpoint stub — Phase-2 placeholder (SPEC.md §3.22).

This route is intentionally unimplemented until pgvector embeddings are computed
in Phase 2.  It returns HTTP 501 with a clear message so callers know it exists
but is not yet available.

Routes:
    POST /search — semantic article search (Phase-2 stub).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from scrapeforge.api.auth import require_api_key

router = APIRouter(tags=["search"])


@router.post("/search")
async def semantic_search(_key: str = Depends(require_api_key)) -> None:  # noqa: B008
    """Semantic search over article embeddings — NOT YET IMPLEMENTED.

    This endpoint is reserved for Phase-2 pgvector integration.  Calling it
    now returns HTTP 501 (Not Implemented).

    Raises:
        HTTPException(501): Always — this is a Phase-2 stub.
    """
    raise HTTPException(
        status_code=501,
        detail="semantic search is a Phase-2 stub (pgvector)",
    )
