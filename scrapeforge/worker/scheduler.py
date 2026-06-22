"""Scheduler for ScrapeForge (W8) — periodic job enqueuer.

Responsibility
--------------
``enqueue_due_sources`` is the testable core: it queries enabled ``Source`` rows,
creates a ``Job`` row per source, and publishes a ``JobMessage`` to the configured
queue.  It is the automated equivalent of ``POST /jobs`` — the scheduler is simply
a time-driven job submitter.

``SchedulerSettings`` is a thin configuration object that documents how arq/deployment
wires the actual cron loop (W9/deployment).  It does NOT require a live arq/Redis
connection at import time — importing this module is always safe.

Design decisions
----------------
- The ``Source`` query is inlined here (``select(Source).where(Source.enabled == True)``
  ordered by id) rather than added to ``repositories.py`` — the contract explicitly
  forbids editing that file (stay-in-your-lane rule, Invariant #17).
- ``session_factory`` is injected (not imported from ``session.py``) so tests can
  supply an ephemeral factory without touching the global engine cache.
- One ``Job`` row + one queue message per enabled source per tick.  No deduplication
  across ticks — each scheduler tick is an independent submission.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from scrapeforge.core.db.models import Source
from scrapeforge.core.db.repositories import create_job
from scrapeforge.core.queue.base import MessageQueue
from scrapeforge.worker.messages import JobMessage


async def _list_enabled_sources(session) -> list[Source]:
    """Return all enabled ``Source`` rows ordered by ``id`` ascending.

    Inlined here per contract — do NOT add this to ``repositories.py``.

    Args:
        session: Open ``AsyncSession``.

    Returns:
        List of ``Source`` ORM instances with ``enabled=True``, ordered by ``id``.
    """
    stmt = select(Source).where(Source.enabled == True).order_by(Source.id)  # noqa: E712
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def enqueue_due_sources(
    *,
    session_factory: async_sessionmaker,
    queue: MessageQueue,
    settings: Any,
) -> int:
    """Open a session, list enabled sources, and enqueue one job per source.

    For each enabled ``Source``:
    1. Generate a unique ``job_id`` (``uuid.uuid4().hex``).
    2. Persist a ``Job`` row via ``create_job`` (status ``'queued'``).
    3. Publish a ``JobMessage`` to ``settings.JOB_QUEUE``.

    The ``url`` field of the published message is ``source.params.get("url")``
    when present, falling back to ``source.name`` when absent.

    Args:
        session_factory: ``async_sessionmaker`` bound to the target database.
        queue:           ``MessageQueue`` backend to publish jobs onto.
        settings:        Settings-like object exposing ``JOB_QUEUE: str``.

    Returns:
        Number of jobs enqueued (== number of enabled sources found).
    """
    count = 0

    async with session_factory() as session:
        sources = await _list_enabled_sources(session)

        for source in sources:
            job_id = uuid.uuid4().hex

            # Persist the Job row (mirrors POST /jobs behaviour).
            await create_job(
                session,
                job_id=job_id,
                source=source.name,
                params=source.params,
            )

            # Resolve the URL: prefer params['url'], fall back to source.name.
            url = source.params.get("url") or source.name

            # Publish the JobMessage.
            message: JobMessage = {
                "job_id": job_id,
                "url": url,
                "bucket": source.bucket,
            }
            await queue.publish(settings.JOB_QUEUE, message)

            count += 1

    return count


class SchedulerSettings:
    """Thin arq cron configuration object (documentation + deployment hook).

    This class documents *how* the cron loop should be wired by W9/deployment.
    It does NOT require a live arq/Redis connection at import time — importing
    ``scrapeforge.worker.scheduler`` is always safe.

    Usage (arq WorkerSettings, wired in W9)::

        import arq
        from scrapeforge.worker.scheduler import enqueue_due_sources, SchedulerSettings

        class WorkerSettings:
            cron_jobs = [
                arq.cron(
                    SchedulerSettings.cron_fn,
                    minute={0, 15, 30, 45},  # every 15 minutes
                )
            ]

    The testable unit is ``enqueue_due_sources``; this class only carries the
    cron function reference and default schedule so deployment has a single place
    to import from.
    """

    #: Default cron schedule: every 15 minutes.
    #: Deployment overrides this via arq WorkerSettings.cron_jobs.
    DEFAULT_CRON_MINUTE = {0, 15, 30, 45}

    @staticmethod
    async def cron_fn(ctx: dict) -> None:
        """arq cron entry-point — called by the arq cron loop on schedule.

        Expects ``ctx`` to carry ``session_factory``, ``queue``, and ``settings``
        keys (injected by the arq startup hook in W9/deployment).  The cron
        function is kept intentionally thin: it delegates all logic to
        ``enqueue_due_sources``.

        Args:
            ctx: arq worker context dict populated by the ``on_startup`` hook.
        """
        await enqueue_due_sources(
            session_factory=ctx["session_factory"],
            queue=ctx["queue"],
            settings=ctx["settings"],
        )
