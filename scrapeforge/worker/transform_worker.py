"""Transform-stage worker (W6) — idempotent read-extract-upsert pipeline.

This module is the transform stage of the ingestion pipeline.  It reads a
raw HTML payload from the object store (via the claim-check ``ResultPointer``),
validates/extracts/normalises the content, UPSERTs a structured article row,
and owns the full Job-status lifecycle (queued → running → done | error).

Design invariants
-----------------
- **Sole Job-status writer**: the scraper worker (W5) never touches the DB;
  the transform worker is the ONLY code path that transitions a Job.
- **Idempotent**: repeated delivery of the same pointer produces exactly one
  article row (``PostgresSink`` uses ``ON CONFLICT (id) DO UPDATE``).
- **Async-first**: all I/O is ``await``-ed; no blocking calls on the event loop.
- **Typed exceptions only**: no bare ``except:``; errors are caught by specific
  type or re-raised to the caller with context.

Public API
----------
``_selectors_for(domain)``
    Resolve CSS selectors for *domain* via the registry; fall back to
    ``PublicScraper`` generic chains.

``handle_result_pointer(pointer, *, store, session_factory)``
    Process one ``ResultPointer`` end-to-end.

``run_transform_worker(*, queue, store, session_factory, settings)``
    Drain the RESULTS queue until empty, calling ``handle_result_pointer``
    for each message.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.repositories import update_job_status
from scrapeforge.core.models import Article as ArticleDTO
from scrapeforge.core.models import ScrapeResult
from scrapeforge.core.objectstore.base import ObjectNotFound, ObjectStore
from scrapeforge.core.queue.base import MessageQueue
from scrapeforge.core.registry import discover_scrapers, get_scraper_for
from scrapeforge.core.storage.postgres import PostgresSink
from scrapeforge.scrapers.public.public import PublicScraper
from scrapeforge.utils.parsers import extract
from scrapeforge.utils.validators import response_is_valid
from scrapeforge.worker.messages import ResultPointer

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selector resolution
# ---------------------------------------------------------------------------


def _selectors_for(domain: str) -> dict[str, str]:
    """Return CSS selectors appropriate for *domain*.

    Resolution order:
    1. Run ``discover_scrapers()`` so all ``@register_scraper`` decorators
       have been applied (idempotent — fast no-op after the first call).
    2. Look up the domain in the registry via ``get_scraper_for``.
    3. If a registered scraper class is found, instantiate it (no I/O — the
       constructor only sets up the semaphore and stores arguments) and call
       ``_get_selectors()``.
    4. Fall back to ``PublicScraper()._get_selectors()`` when no domain-specific
       class is registered.

    Returns
    -------
    dict[str, str]
        Always a non-empty dict (``PublicScraper`` guarantees ``title`` and
        ``content`` keys at minimum).

    HTML-only scope
    ---------------
    This function is HTML-extraction-oriented.  A registered scraper whose raw
    payload is *not* HTML (e.g. a hypothetical ``RedditScraper`` that stores
    JSON) typically returns ``{}`` from ``_get_selectors()``.  When the selector
    dict is empty (or lacks a ``'content'`` key), ``response_is_valid`` returns
    ``False`` and the Job is marked ``'error'`` with "soft block or challenge
    page detected".  This is the correct terminal outcome for Phase 1 — JSON /
    bucket-specific transforms are a later enhancement; only HTML-extractable
    domains are transform-eligible at this stage.
    """
    discover_scrapers()

    scraper_cls = get_scraper_for(domain)
    instance = scraper_cls() if scraper_cls is not None else PublicScraper()

    return instance._get_selectors()


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------


async def handle_result_pointer(
    pointer: ResultPointer,
    *,
    store: ObjectStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Process one ``ResultPointer`` end-to-end.

    Steps
    -----
    1. Mark Job **running** (started_at set).
    2. If ``pointer['status'] != 'success'``, mark Job **error** and return.
    3. Fetch raw HTML from *store* at ``pointer['object_key']``.
       If absent → mark Job **error** ("raw payload missing") and return.
    4. Validate the HTML with ``response_is_valid``.
       If invalid (soft block / challenge signature) → mark Job **error** and return.
    5. Extract fields via ``parsers.extract``.
    6. Build an ``ArticleDTO`` with ``raw_key`` and ``bucket`` in metadata so
       ``PostgresSink.write`` stores the claim-check pointer durably.
    7. UPSERT via ``PostgresSink.write`` (idempotent on URL PK).
    8. Mark Job **done** (result_count=1, finished_at set).

    Parameters
    ----------
    pointer:
        ``ResultPointer`` from the RESULTS queue.
    store:
        Object-store backend.  ``store.get(object_key)`` returns raw bytes or
        raises ``ObjectNotFound``.
    session_factory:
        ``async_sessionmaker`` for the serving DB.  A new session is opened
        for each ``update_job_status`` call so partial failures leave a clean
        transactional footprint.
    """
    job_id: str = pointer["job_id"]
    url: str = pointer["url"]
    domain: str = pointer["domain"]
    bucket: str = pointer["bucket"]
    object_key: str = pointer["object_key"]
    pointer_status: str = pointer["status"]

    # ------------------------------------------------------------------
    # Step 1: mark Job running
    # ------------------------------------------------------------------
    async with session_factory() as session:
        await update_job_status(
            session,
            job_id,
            status="running",
            started=True,
        )

    # ------------------------------------------------------------------
    # Step 2: non-success pointer → error, stop
    # ------------------------------------------------------------------
    if pointer_status != "success":
        error_msg = f"scraper returned status '{pointer_status}'"
        log.warning("transform: job=%s url=%s %s", job_id, url, error_msg)
        async with session_factory() as session:
            await update_job_status(
                session,
                job_id,
                status="error",
                result_count=0,
                error=error_msg,
                finished=True,
            )
        return

    # ------------------------------------------------------------------
    # Step 3: fetch raw HTML from object store
    # ------------------------------------------------------------------
    try:
        raw_bytes = await store.get(object_key)
    except ObjectNotFound:
        error_msg = "raw payload missing"
        log.error("transform: job=%s url=%s %s (key=%s)", job_id, url, error_msg, object_key)
        async with session_factory() as session:
            await update_job_status(
                session,
                job_id,
                status="error",
                result_count=0,
                error=error_msg,
                finished=True,
            )
        return

    # Decode with errors="replace" so latin-1 / mislabeled-charset payloads
    # flow through to validation and extraction rather than raising an
    # unhandled UnicodeDecodeError that would leave the Job stuck 'running'
    # and cause the message to requeue in a poison loop.
    html = raw_bytes.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Step 4: validate (soft-block / challenge-signature check)
    # ------------------------------------------------------------------
    selectors = _selectors_for(domain)
    if not response_is_valid(html, selectors):
        error_msg = "soft block or challenge page detected"
        log.warning("transform: job=%s url=%s %s", job_id, url, error_msg)
        async with session_factory() as session:
            await update_job_status(
                session,
                job_id,
                status="error",
                result_count=0,
                error=error_msg,
                finished=True,
            )
        return

    # ------------------------------------------------------------------
    # Step 5: extract fields
    # ------------------------------------------------------------------
    fields = extract(html, selectors)

    # ------------------------------------------------------------------
    # Step 6: build ArticleDTO
    #
    # Include ``raw_key`` and ``bucket`` in metadata so PostgresSink stores
    # the claim-check pointer durably (``raw_key`` column + ``meta`` JSONB).
    # ------------------------------------------------------------------
    article = ArticleDTO(
        url=url,
        title=fields.get("title") or "",
        content=fields.get("content") or "",
        author=fields.get("author"),
        publish_date=None,
        raw_html=None,
        metadata={
            "source_domain": domain,
            "bucket": bucket,
            "raw_key": object_key,
            "fetched_at": pointer.get("fetched_at", datetime.now(UTC).isoformat()),
        },
    )

    # ------------------------------------------------------------------
    # Step 7: UPSERT via PostgresSink (idempotent on URL PK)
    # ------------------------------------------------------------------
    sink = PostgresSink(session_factory)
    result = ScrapeResult(
        status="success",
        driver_used="transform",
        article=article,
    )
    await sink.write(result)

    # ------------------------------------------------------------------
    # Step 8: mark Job done
    # ------------------------------------------------------------------
    async with session_factory() as session:
        await update_job_status(
            session,
            job_id,
            status="done",
            result_count=1,
            finished=True,
        )

    log.info("transform: job=%s url=%s done", job_id, url)


# ---------------------------------------------------------------------------
# Drain loop
# ---------------------------------------------------------------------------


async def run_transform_worker(
    *,
    queue: MessageQueue,
    store: ObjectStore,
    session_factory: async_sessionmaker[AsyncSession],
    settings,
) -> None:
    """Drain the RESULTS queue until empty.

    Calls ``queue.consume_once`` in a loop until the queue is empty
    (``consume_once`` returns ``False``).

    Each message is delivered to ``handle_result_pointer`` which owns the
    full Job-status lifecycle and UPSERT.

    Parameters
    ----------
    queue:
        Message-queue backend.  Must implement ``consume_once``.
    store:
        Object-store backend passed through to ``handle_result_pointer``.
    session_factory:
        ``async_sessionmaker`` passed through to ``handle_result_pointer``.
    settings:
        Settings-like object; must expose ``RESULTS_QUEUE`` (str) and
        ``QUEUE_MAX_RETRIES`` (int).
    """

    def _make_handler():
        async def _handler(msg: dict) -> None:
            await handle_result_pointer(
                msg,  # type: ignore[arg-type]
                store=store,
                session_factory=session_factory,
            )

        return _handler

    handler = _make_handler()

    while await queue.consume_once(
        settings.RESULTS_QUEUE,
        handler,
        max_retries=settings.QUEUE_MAX_RETRIES,
    ):
        pass  # keep draining until the queue is empty
