"""Typed async repository functions for the API and worker planes (SPEC.md §3.21).

All public functions accept an ``AsyncSession`` as their first positional argument
and delegate all persistence to SQLAlchemy — no raw SQL in API routes (the API
layer calls these functions, not ``session.execute(text(...))``).

SRP: this module owns *query logic only*.  Model definitions live in ``models.py``;
engine / session-factory wiring lives in ``session.py``.

Naming conventions
------------------
- ``get_*``    — fetch a single row by primary key; returns ``Model | None``.
- ``list_*``   — return a ``list[Model]`` (ordered, paginated).
- ``query_*``  — filtered list query (multiple filter parameters).
- ``create_*`` — insert a new row and return the ORM instance.
- ``update_*`` — mutate an existing row and commit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scrapeforge.core.db.models import Article, Job

# ---------------------------------------------------------------------------
# Article queries
# ---------------------------------------------------------------------------


async def get_article(session: AsyncSession, article_id: str) -> Article | None:
    """Return the ``Article`` with primary key *article_id*, or ``None``.

    Args:
        session:    Open ``AsyncSession``.
        article_id: SHA-256 hex digest of the article URL.

    Returns:
        The ``Article`` row, or ``None`` if not found.
    """
    return await session.get(Article, article_id)


async def query_articles(
    session: AsyncSession,
    *,
    domain: str | None = None,
    bucket: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Article]:
    """Return a filtered, paginated list of ``Article`` rows.

    Filters are additive (AND).  Results are ordered by ``fetched_at`` descending
    so the most-recently fetched articles appear first.  A secondary ``id``
    tiebreaker makes pagination deterministic when multiple rows share the same
    ``fetched_at`` value (common in tests and bulk imports).

    Args:
        session: Open ``AsyncSession``.
        domain:  Filter by exact domain match (e.g. ``'ft.com'``).
        bucket:  Filter by bucket (``'premium'``, ``'community'``, ``'public'``).
        since:   Include only articles with ``fetched_at >= since``.
        limit:   Maximum number of rows to return (default 50).
        offset:  Number of rows to skip for pagination (default 0).

    Returns:
        List of matching ``Article`` ORM instances.
    """
    stmt = select(Article).order_by(Article.fetched_at.desc(), Article.id)

    if domain is not None:
        stmt = stmt.where(Article.domain == domain)
    if bucket is not None:
        stmt = stmt.where(Article.bucket == bucket)
    if since is not None:
        stmt = stmt.where(Article.fetched_at >= since)

    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Job queries
# ---------------------------------------------------------------------------


async def create_job(
    session: AsyncSession,
    *,
    job_id: str,
    source: str,
    params: dict,
) -> Job:
    """Insert a new ``Job`` row with ``status='queued'`` and ``created_at=now(UTC)``.

    Args:
        session:  Open ``AsyncSession``.
        job_id:   UUID string supplied by the caller.
        source:   Platform / domain / ``'url-list'`` string.
        params:   Job parameters dict (``urls``, ``bucket``, ``limit``, …).

    Returns:
        The newly committed ``Job`` instance.
    """
    job = Job(
        id=job_id,
        status="queued",
        source=source,
        params=params,
        created_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_job(session: AsyncSession, job_id: str) -> Job | None:
    """Return the ``Job`` with primary key *job_id*, or ``None``.

    Args:
        session: Open ``AsyncSession``.
        job_id:  UUID string of the job.

    Returns:
        The ``Job`` row, or ``None`` if not found.
    """
    return await session.get(Job, job_id)


async def list_jobs(session: AsyncSession, *, limit: int = 50) -> list[Job]:
    """Return the most recently created ``Job`` rows (``created_at`` descending).

    Results are ordered by ``created_at`` descending.  A secondary ``id``
    tiebreaker makes ordering deterministic when multiple jobs share the same
    ``created_at`` value (common in tests).

    Args:
        session: Open ``AsyncSession``.
        limit:   Maximum number of rows to return (default 50).

    Returns:
        List of ``Job`` ORM instances, newest first.
    """
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_job_status(
    session: AsyncSession,
    job_id: str,
    *,
    status: str,
    result_count: int | None = None,
    error: str | None = None,
    started: bool = False,
    finished: bool = False,
) -> None:
    """Update the status (and optionally timestamps / counts) of a ``Job``.

    Commits the changes to the database before returning.

    Args:
        session:      Open ``AsyncSession``.
        job_id:       UUID string identifying the job to update.
        status:       New status value (``'queued'``, ``'running'``, ``'done'``, ``'error'``).
        result_count: If provided, overwrite the job's ``result_count``.
        error:        If provided, set the job's ``error`` message.
        started:      If ``True``, set ``started_at`` to the current UTC time.
        finished:     If ``True``, set ``finished_at`` to the current UTC time.

    Raises:
        ValueError: If the job does not exist.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job {job_id!r} not found")

    job.status = status

    now = datetime.now(UTC)
    if started:
        job.started_at = now
    if finished:
        job.finished_at = now
    if result_count is not None:
        job.result_count = result_count
    if error is not None:
        job.error = error

    await session.commit()
