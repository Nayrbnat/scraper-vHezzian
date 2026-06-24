"""Testable run-once job functions (no Typer): schema init + Substack ingest.

These are the deploy/cron jobs in pure-async form so they can be unit-tested with injected
fakes; ``pipeline/cli.py`` wraps them with the real adapters.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from scrapeforge.core.db.migrations import ensure_summary_columns
from scrapeforge.core.db.models import Base
from scrapeforge.core.storage.postgres import PostgresSink

log = logging.getLogger(__name__)


async def init_db(engine: AsyncEngine) -> None:
    """Idempotently prepare a database: pgvector extension + tables + Phase-2 columns.

    Safe to re-run. Mirrors the test harness's schema bootstrap so a fresh Neon DB is ready.
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    await ensure_summary_columns(engine)
    log.info("init_db: schema ready (vector ext + tables + summary columns)")


async def ingest_publications(*, session_factory, scraper, sources, limit: int) -> int:
    """Scrape each publication and UPSERT its successful articles into Postgres.

    Lean path — no queue, no object store. Reuses ``scrape_publication`` + ``PostgresSink``
    (idempotent UPSERT on sha256(url)). Returns the number of articles persisted.
    """
    sink = PostgresSink(session_factory)
    persisted = 0
    for source in sources:
        results = await scraper.scrape_publication(source.base, limit=limit)
        for result in results:
            if result.status != "success" or result.article is None:
                continue
            if sink.seen(result.article.url):
                continue
            await sink.write(result)
            persisted += 1
    log.info("ingest: persisted %d articles across %d publications", persisted, len(sources))
    return persisted
