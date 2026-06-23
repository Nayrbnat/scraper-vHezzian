"""Community-ingestion worker (Phase 1, lean) — scheduled publication scrape → Postgres.

Consumes an ``IngestMessage`` from the INGEST queue, runs the community bucket scraper's
``scrape_publication`` (which already returns fully-parsed ``Article``s), archives each new
post's raw payload to the object store (claim-check), and persists structured rows via the
existing idempotent ``PostgresSink``.  Owns the full Job lifecycle for community sources
(queued → running → done | error).

Invariant #18 carve-out: fully-parsing community/JSON scrapers persist in-worker; the
scraper→transform HTML claim-check split governs *public-bucket HTML* only.  Re-extracting
Substack's JSON-sourced fields via CSS selectors would lose title/author/date, so this worker
deliberately writes the scraper's already-parsed article straight to the sink.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.repositories import update_job_status
from scrapeforge.core.objectstore.base import ObjectStore
from scrapeforge.core.queue.base import MessageQueue
from scrapeforge.core.storage.base import url_id
from scrapeforge.core.storage.postgres import PostgresSink
from scrapeforge.worker.messages import IngestMessage, raw_object_key

log = logging.getLogger(__name__)


def _resolve_scraper(platform: str):
    """Lazy-resolve the community scraper for *platform* (no eager bucket imports).

    Mirrors the CLI's platform dispatch so this worker doesn't import every bucket at
    module load.  Reddit slots in later by adding one branch.
    """
    if platform == "substack":
        from scrapeforge.scrapers.community.substack import SubstackScraper

        return SubstackScraper()
    raise ValueError(f"no community-ingest scraper for platform {platform!r}")


async def handle_ingest_job(
    payload: IngestMessage,
    *,
    store: ObjectStore,
    session_factory: async_sessionmaker[AsyncSession],
    scraper=None,
) -> int:
    """Scrape one publication and persist its successful articles. Returns # persisted.

    Steps: mark Job running → run ``scrape_publication`` → for each ``success`` article:
    skip if already seen this run, archive raw (claim-check), UPSERT via ``PostgresSink`` →
    mark Job done.  A raised scrape/persist error marks the Job ``error`` and re-raises so the
    ``MessageQueue`` retries → DLQ.

    Args:
        payload:         The ``IngestMessage`` from the INGEST queue.
        store:           Object-store backend (raw archive).
        session_factory: ``async_sessionmaker`` for the serving DB.
        scraper:         Optional injected scraper (tests); resolved by platform otherwise.
    """
    job_id = payload["job_id"]
    target = payload["target"]
    bucket = payload["bucket"]
    limit = payload["limit"]

    scraper = scraper if scraper is not None else _resolve_scraper(payload["platform"])

    async with session_factory() as session:
        await update_job_status(session, job_id, status="running", started=True)

    sink = PostgresSink(session_factory)
    persisted = 0
    try:
        results = await scraper.scrape_publication(target, limit=limit)
        for result in results:
            if result.status != "success" or result.article is None:
                continue
            article = result.article
            if sink.seen(article.url):
                continue
            key = raw_object_key(bucket, url_id(article.url))
            if article.raw_html:
                raw = article.raw_html.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                raw = json.dumps(
                    {"status": result.status, "url": article.url, "title": article.title}
                ).encode("utf-8")
                content_type = "application/json"
            await store.put(key, raw, content_type)
            # Carry the claim-check pointer into the persisted row's metadata.
            article.metadata.setdefault("raw_key", key)
            await sink.write(result)
            persisted += 1
    except Exception as exc:  # noqa: BLE001
        async with session_factory() as session:
            await update_job_status(
                session,
                job_id,
                status="error",
                result_count=persisted,
                error=str(exc),
                finished=True,
            )
        raise  # let the MessageQueue retry → DLQ

    async with session_factory() as session:
        await update_job_status(
            session, job_id, status="done", result_count=persisted, finished=True
        )
    log.info("community-ingest: job=%s target=%s persisted=%d", job_id, target, persisted)
    return persisted


async def run_community_ingest_worker(
    *,
    queue: MessageQueue,
    store: ObjectStore,
    session_factory: async_sessionmaker[AsyncSession],
    settings,
) -> None:
    """Drain the INGEST queue until empty (Phase-1 drain loop).

    Each message is handled by ``handle_ingest_job``; retry/DLQ is delegated to the
    ``MessageQueue`` port (``consume_once``).
    """

    async def _handler(msg: dict) -> None:
        await handle_ingest_job(
            msg,  # type: ignore[arg-type]
            store=store,
            session_factory=session_factory,
        )

    while await queue.consume_once(
        settings.INGEST_QUEUE, _handler, max_retries=settings.QUEUE_MAX_RETRIES
    ):
        pass  # keep draining until the queue is empty
