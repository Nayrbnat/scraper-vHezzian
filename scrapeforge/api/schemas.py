"""Pydantic v2 request / response schemas for the ScrapeForge API (SPEC.md §3.22).

All output schemas use ``ConfigDict(from_attributes=True)`` so they can be
constructed directly from SQLAlchemy ORM instances returned by the repository layer.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Article schemas
# ---------------------------------------------------------------------------


class ArticleOut(BaseModel):
    """Response schema for a single scraped article."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    domain: str
    bucket: str
    title: str
    content: str
    author: str | None
    publish_date: datetime | None
    fetched_at: datetime
    raw_key: str | None
    meta: dict


# ---------------------------------------------------------------------------
# Job schemas
# ---------------------------------------------------------------------------


class JobIn(BaseModel):
    """Request body for POST /jobs."""

    source: str
    """Platform, domain, or ``'url-list'`` identifier for the scrape target."""

    urls: list[str] | None = None
    """Optional explicit list of URLs to scrape."""

    bucket: str | None = None
    """Optional bucket hint (``'premium'``, ``'community'``, ``'public'``)."""

    limit: int | None = None
    """Optional cap on the number of articles to fetch."""


class JobOut(BaseModel):
    """Response schema for a scrape job."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    source: str
    result_count: int
    created_at: datetime
    error: str | None = None
