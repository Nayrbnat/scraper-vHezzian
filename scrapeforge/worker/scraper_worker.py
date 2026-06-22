"""Scraper-stage worker (W5) — stateless fetch → archive → publish.

This module is the ingestion pipeline's scraper stage.  It is deliberately
DB-free: the ``handle_scrape_job`` handler writes raw bytes to the object store
and publishes a ``ResultPointer`` message; it **never** touches the serving DB
(no ``PostgresSink``, no ``Job`` row, no ORM import).  All Job-status updates
are owned by the transform worker (W6).

Architecture
------------
``handle_scrape_job``
    Pure async handler.  Accepts a ``JobMessage`` dict and in-memory fakes so
    it is fully testable without I/O.  Side effects: one object-store PUT + one
    queue publish.

``run_scraper_worker``
    Thin drain loop.  Calls ``queue.consume_once`` in a loop until the job
    queue is empty.  Production wiring to arq / long-running consumption is
    deferred to W9 (deployment).
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime

from scrapeforge.core.objectstore.base import ObjectStore
from scrapeforge.core.queue.base import MessageQueue
from scrapeforge.core.storage.base import url_id
from scrapeforge.worker.messages import JobMessage, ResultPointer, raw_object_key


async def handle_scrape_job(
    payload: JobMessage,
    *,
    engine,
    store: ObjectStore,
    queue: MessageQueue,
    results_queue: str,
) -> ResultPointer:
    """Fetch one URL, archive the raw payload, publish a pointer.

    Parameters
    ----------
    payload:
        ``JobMessage`` dict from the JOB queue — carries ``job_id``, ``url``,
        and an optional ``bucket`` hint.
    engine:
        Any object with ``async def scrape(url: str) -> ScrapeResult``.  The
        real engine is ``ScrapeEngine``; tests pass a lightweight fake.
    store:
        Object-store backend (``ObjectStore`` port).  The raw payload is PUT
        here under the deterministic key ``raw_object_key(bucket, url_id(url))``.
    queue:
        Message-queue backend (``MessageQueue`` port).  The ``ResultPointer``
        is published to *results_queue*.
    results_queue:
        Name of the results queue to publish the pointer onto.

    Returns
    -------
    ResultPointer
        The pointer that was published (handy for tests and logging).
    """
    job_id: str = payload["job_id"]
    url: str = payload["url"]
    bucket_hint: str | None = payload.get("bucket")

    # 1. Fetch via the engine (curl_cffi-backed in production).
    result = await engine.scrape(url)

    # 2. Determine raw bytes + content-type.
    if result.article is not None and result.article.raw_html:
        raw: bytes = result.article.raw_html.encode("utf-8")
        content_type = "text/html; charset=utf-8"
    else:
        # Archive a JSON fallback so the raw zone always records every attempt.
        fallback = {
            "status": result.status,
            "error": result.error,
            "url": url,
        }
        raw = json.dumps(fallback).encode("utf-8")
        content_type = "application/json"

    # 3. Resolve bucket: job hint → article metadata → "public".
    uid = url_id(url)
    article_bucket: str | None = result.article.metadata.get("bucket") if result.article else None
    bkt: str = bucket_hint or article_bucket or "public"
    object_key = raw_object_key(bkt, uid)

    # 4. Idempotent claim-check PUT.
    await store.put(object_key, raw, content_type)

    # 5. Build pointer.
    domain = urllib.parse.urlsplit(url).hostname or ""
    pointer = ResultPointer(
        job_id=job_id,
        object_key=object_key,
        url=url,
        url_id=uid,
        domain=domain,
        bucket=bkt,
        status=result.status,
        fetched_at=datetime.now(UTC).isoformat(),
    )

    # 6. Publish pointer onto results queue.
    await queue.publish(results_queue, dict(pointer))

    return pointer


async def run_scraper_worker(
    *,
    queue: MessageQueue,
    store: ObjectStore,
    engine,
    settings,
) -> None:
    """Drain the JOB queue synchronously (Phase-6 drain loop).

    Calls ``queue.consume_once`` in a loop until the queue is empty
    (``consume_once`` returns ``False``).

    Note
    ----
    This is a simple drain loop suitable for Phase 6 end-to-end testing and
    CLI invocation.  Long-running consumption via arq (Redis-backed) is wired
    in deployment (W9) and replaces this loop with an arq worker function.
    """

    async def _handler(msg: dict) -> None:
        await handle_scrape_job(
            msg,  # type: ignore[arg-type]
            engine=engine,
            store=store,
            queue=queue,
            results_queue=settings.RESULTS_QUEUE,
        )

    # Termination guarantee: consume_once delegates retry/DLQ to MessageQueue, so a
    # poison message that keeps raising will be dead-lettered after max_retries+1
    # attempts rather than spinning forever.  The loop exits when the queue is empty
    # (consume_once returns False).
    while await queue.consume_once(
        settings.JOB_QUEUE,
        _handler,
        max_retries=settings.QUEUE_MAX_RETRIES,
    ):
        pass  # keep draining until the queue is empty
