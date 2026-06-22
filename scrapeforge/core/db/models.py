"""SQLAlchemy 2.0 async ORM models for ScrapeForge (SPEC.md §3.21).

Three tables:
- ``articles``  — deduplicated scraped article records; PK = sha256(url).
- ``jobs``      — scrape job lifecycle tracking (queued → running → done|error).
- ``sources``   — scheduled scrape targets (optional; included per contract).

All ``datetime`` columns use ``DateTime(timezone=True)`` so Postgres stores
``TIMESTAMP WITH TIME ZONE`` and round-trips timezone-aware values correctly.
``datetime.utcnow()`` is deprecated in Python 3.12+ and is explicitly forbidden
here; use ``datetime.now(UTC)`` instead.

The ``meta`` column (JSONB) stores provenance metadata (driver_used, proxy_used,
etc.).  It is intentionally named ``meta`` — SQLAlchemy's ``DeclarativeBase``
reserves the attribute name ``metadata`` on every mapped class.

The ``embedding`` column (``pgvector`` ``Vector(1536)``) is included for Phase-2
RAG; it stays ``NULL`` until embeddings are computed.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all ScrapeForge ORM models."""


class Article(Base):
    """A scraped article persisted in the ``articles`` table.

    The primary key ``id`` is ``sha256(url)`` (hex digest, 64 chars), which
    makes the PK constraint the deduplication gate — duplicate inserts raise
    ``IntegrityError`` on plain ``INSERT``.  ``PostgresSink.write()`` uses an
    ``ON CONFLICT DO UPDATE`` UPSERT (W4) so the constraint is never violated
    in the production write path; tests verify the raw constraint here.
    """

    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(primary_key=True)
    """sha256(url) — 64-char hex digest; PK constraint enforces dedup."""

    url: Mapped[str]
    """The canonical URL of the scraped page."""

    domain: Mapped[str] = mapped_column(index=True)
    """Registered domain extracted from the URL (e.g. ``ft.com``).

    ``index=True`` creates a B-tree index (``ix_articles_domain``).  No extra
    ``Index(...)`` entry in ``__table_args__`` — that would create a duplicate.
    """

    bucket: Mapped[str]
    """Scraper bucket: ``'premium'``, ``'community'``, or ``'public'``."""

    title: Mapped[str]
    """Extracted article headline."""

    content: Mapped[str] = mapped_column(Text)
    """Cleaned article body text or Markdown."""

    author: Mapped[str | None]
    """Byline author string, or ``None`` if not present."""

    publish_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Timezone-aware publication date (``TIMESTAMP WITH TIME ZONE``), or ``None``."""

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """Timezone-aware UTC timestamp (``TIMESTAMP WITH TIME ZONE``) when the row was written."""

    raw_key: Mapped[str | None]
    """Object-store pointer to the raw payload (claim-check pattern)."""

    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    """Provenance metadata: ``driver_used``, ``proxy_used``, etc.

    Intentionally named ``meta`` — SQLAlchemy reserves ``metadata`` on every
    ``DeclarativeBase`` subclass.
    """

    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    """pgvector embedding (1536-dim, OpenAI-compatible).  NULL until Phase-2 RAG."""


class Job(Base):
    """Tracks the lifecycle of a scrape job in the ``jobs`` table.

    Status flow::

        queued → running → done
                        → error
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(primary_key=True)
    """UUID string supplied by the caller (API or test)."""

    status: Mapped[str] = mapped_column(default="queued")
    """Current state: ``'queued'``, ``'running'``, ``'done'``, or ``'error'``."""

    source: Mapped[str]
    """Platform, domain, or ``'url-list'`` that originated the job."""

    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    """Job parameters: ``{urls?, bucket?, limit?}``."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """Timezone-aware UTC timestamp (``TIMESTAMP WITH TIME ZONE``) when the job was created."""

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Set when the worker picks up the job (``status → 'running'``)."""

    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Set when the job reaches a terminal state (``'done'`` or ``'error'``)."""

    error: Mapped[str | None]
    """Error message if ``status == 'error'``; ``None`` otherwise."""

    result_count: Mapped[int] = mapped_column(default=0)
    """Number of successfully scraped articles."""


class Source(Base):
    """Scheduled scrape target in the ``sources`` table.

    Optional per contract but included for completeness.  The scheduler
    reads enabled sources and enqueues periodic ``Job`` rows.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    """Auto-incrementing integer PK."""

    name: Mapped[str] = mapped_column(unique=True)
    """Human-readable name; must be unique (e.g. ``'ft.com-daily'``)."""

    bucket: Mapped[str]
    """Scraper bucket: ``'premium'``, ``'community'``, or ``'public'``."""

    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    """Scheduler parameters forwarded to the Job (urls, limit, …)."""

    cron: Mapped[str | None]
    """Cron expression for recurrence, or ``None`` for manual-only."""

    enabled: Mapped[bool] = mapped_column(default=True)
    """Whether the scheduler should enqueue this source automatically."""
