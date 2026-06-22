"""Shared message contracts for the ingestion pipeline (Phase 6).

Both worker stages agree on these dict shapes so the producer (scraper worker) and the
consumer (transform worker) never drift.  Messages are plain JSON-serialisable dicts (the
``MessageQueue`` port carries ``dict`` payloads).

Flow:
    API ──JobMessage──► JOB queue ──► scraper worker
        scraper writes raw → object store at ``raw_object_key(bucket, url_id)``
        scraper ──ResultPointer──► RESULTS queue ──► transform worker
            transform reads raw via the pointer's ``object_key`` → UPSERT structured row
"""

from __future__ import annotations

from typing import TypedDict


class JobMessage(TypedDict):
    """JOB-queue payload: one scrape unit (API → scraper worker).

    Phase 6 uses one URL per job for a clean end-to-end; ``bucket`` is an optional hint
    (the engine routes by domain regardless).
    """

    job_id: str
    url: str
    bucket: str | None


class ResultPointer(TypedDict):
    """RESULTS-queue payload (claim-check): a pointer to the archived raw payload.

    Small by design — the raw bytes live in the object store at ``object_key``, never on
    the bus.  The transform worker reads ``object_key`` to fetch the raw payload.
    """

    job_id: str
    object_key: str  # object-store key of the raw payload
    url: str
    url_id: str  # sha256(url) — the structured-row PK / dedup key
    domain: str
    bucket: str
    status: str  # the scraper's ScrapeResult.status ('success' | 'challenge' | ...)
    fetched_at: str  # ISO-8601 UTC timestamp


def raw_object_key(bucket: str, url_id: str) -> str:
    """Deterministic object-store key for a raw payload.

    Deterministic (a function of ``url_id``) so re-scraping the same URL overwrites the
    same key — an idempotent claim-check PUT.
    """
    return f"raw/{bucket or 'unknown'}/{url_id}"
