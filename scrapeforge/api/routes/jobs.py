"""Job submission and status endpoints — requires API key auth (SPEC.md §3.22).

Invariant #18: This module MUST NOT import ScrapeEngine, StealthBridge, any driver
class, or any worker handler.  It is the read + enqueue plane only:
  1. Persist a Job row via the repository.
  2. Publish a JobMessage to the queue.
  3. Return JobOut.
An AST test in tests/test_api_jobs.py enforces this statically.

Routes:
    POST /jobs          — submit a new scrape job (202 Accepted).
    GET  /jobs/{job_id} — fetch job status by id.
    GET  /jobs          — list recent jobs.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from scrapeforge.api.auth import require_api_key
from scrapeforge.api.deps import get_queue, get_session, get_settings
from scrapeforge.api.schemas import JobIn, JobOut
from scrapeforge.core.db.repositories import create_job, get_job, list_jobs
from scrapeforge.core.queue.base import MessageQueue

router = APIRouter(tags=["jobs"])


@router.post("/jobs", status_code=202, response_model=JobOut)
async def submit_job(
    body: JobIn,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: MessageQueue = Depends(get_queue),  # noqa: B008
    settings=Depends(get_settings),  # noqa: B008
    _key: str = Depends(require_api_key),  # noqa: B008
) -> JobOut:
    """Submit a new scrape job.

    Persists a ``Job`` row with ``status='queued'`` and publishes a ``JobMessage``
    to the job queue.  Returns immediately with the new job's details (202 Accepted).

    The worker plane picks up the message and drives the actual scraping —
    this endpoint is **enqueue only** (Invariant #18).
    """
    job_id = uuid.uuid4().hex

    # Persist the job row
    job = await create_job(
        session,
        job_id=job_id,
        source=body.source,
        params={
            "urls": body.urls,
            "bucket": body.bucket,
            "limit": body.limit,
        },
    )

    # Publish to the job queue (worker picks this up; API never touches the engine).
    #
    # Phase 6 message shape: ``url`` carries ``body.source`` (the single fetch target).
    # ``body.urls`` is persisted in ``Job.params`` above but intentionally omitted from
    # the queue message — multi-URL expansion is reserved for a future phase where the
    # worker will fan out one message per URL.  ``source`` is the Phase-6 single URL.
    await queue.publish(
        settings.JOB_QUEUE,
        {
            "job_id": job_id,
            "url": body.source,
            "bucket": body.bucket,
        },
    )

    return JobOut.model_validate(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_by_id(
    job_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    _key: str = Depends(require_api_key),  # noqa: B008
) -> JobOut:
    """Return the status and details of a single job.

    Raises:
        HTTPException(404): If no job with the given id exists.
    """
    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return JobOut.model_validate(job)


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs_endpoint(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    _key: str = Depends(require_api_key),  # noqa: B008
) -> list[JobOut]:
    """Return the most-recently created jobs (newest first)."""
    rows = await list_jobs(session, limit=limit)
    return [JobOut.model_validate(row) for row in rows]
