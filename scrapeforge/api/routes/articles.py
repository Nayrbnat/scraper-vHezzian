"""Article read endpoints — requires API key authentication (SPEC.md §3.22).

Routes:
    GET /articles                 — filtered, paginated list of articles.
    GET /articles/{article_id}    — single article by SHA-256 id.

All queries delegate to the repository layer (``core/db/repositories.py``).
No raw SQL in routes (SRP).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException  # noqa: B008
from sqlalchemy.ext.asyncio import AsyncSession

from scrapeforge.api.auth import require_api_key
from scrapeforge.api.deps import get_session
from scrapeforge.api.schemas import ArticleOut
from scrapeforge.core.db.repositories import get_article, query_articles

router = APIRouter(tags=["articles"])


@router.get("/articles", response_model=list[ArticleOut])
async def list_articles(
    domain: str | None = None,
    bucket: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    _key: str = Depends(require_api_key),  # noqa: B008
) -> list[ArticleOut]:
    """Return a filtered, paginated list of scraped articles.

    All filter parameters are optional and additive (AND).  Results are ordered
    by ``fetched_at`` descending (most-recent first).
    """
    rows = await query_articles(
        session,
        domain=domain,
        bucket=bucket,
        since=since,
        limit=limit,
        offset=offset,
    )
    return [ArticleOut.model_validate(row) for row in rows]


@router.get("/articles/{article_id}", response_model=ArticleOut)
async def get_article_by_id(
    article_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    _key: str = Depends(require_api_key),  # noqa: B008
) -> ArticleOut:
    """Return a single article by its SHA-256 id.

    Raises:
        HTTPException(404): If no article with the given id exists.
    """
    row = await get_article(session, article_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"article {article_id!r} not found")
    return ArticleOut.model_validate(row)
