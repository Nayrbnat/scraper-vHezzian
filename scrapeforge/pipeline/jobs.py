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
from scrapeforge.exceptions import ScrapeForgeError

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


async def ingest_publications(
    *, session_factory, scraper, sources, limit: int, via_rss: bool = False
) -> int:
    """Scrape each publication and UPSERT its successful articles into Postgres.

    Lean path — no queue, no object store. Reuses ``PostgresSink`` (idempotent UPSERT on
    sha256(url)). When *via_rss* is set, scrape each publication's RSS feed
    (``scrape_publication_via_rss``) instead of the rate-limited JSON API
    (``scrape_publication``). Returns the number of articles persisted.
    """
    sink = PostgresSink(session_factory)
    persisted = 0
    failed = 0
    for source in sources:
        # One publication's failure (HTTP 429, Cloudflare challenge, driver error) must not
        # abort the whole run — log it and move on so the other publications still ingest.
        try:
            if via_rss:
                results = await scraper.scrape_publication_via_rss(source.base, limit=limit)
            else:
                results = await scraper.scrape_publication(source.base, limit=limit)
        except ScrapeForgeError as exc:
            failed += 1
            log.warning("ingest: skipping %s — scrape failed: %s", source.base, exc)
            continue
        for result in results:
            if result.status != "success" or result.article is None:
                continue
            if sink.seen(result.article.url):
                continue
            await sink.write(result)
            persisted += 1
    log.info(
        "ingest: persisted %d articles across %d publications (%d failed)",
        persisted,
        len(sources),
        failed,
    )
    return persisted


async def ingest_subreddits(
    *,
    session_factory,
    scraper,
    subreddits,
    limit: int,
    sort: str = "hot",
    min_score: int = 25,
) -> int:
    """Scrape each subreddit and UPSERT its quality self-posts into Postgres.

    Mirrors :func:`ingest_publications` for Bucket 2 Reddit: drives the injected scraper's
    ``scrape_subreddit`` and persists via the idempotent ``PostgresSink``. v1 keeps the corpus
    high-signal by persisting only **self-posts with real text** (link posts have empty content —
    fetching the linked article is Bucket-3 work) whose Reddit ``score`` is at least *min_score*
    (cuts low-engagement meme noise). One subreddit's failure (HTTP 429, soft-block) is logged and
    skipped so the rest still ingest. Returns the number of articles persisted.
    """
    sink = PostgresSink(session_factory)
    persisted = 0
    failed = 0
    for source in subreddits:
        try:
            results = await scraper.scrape_subreddit(source.subreddit, limit=limit, sort=sort)
        except Exception as exc:  # noqa: BLE001
            # Broad on purpose: a datacenter-IP soft-block (the GitHub Actions case) can return a
            # non-JSON challenge body or a payload without the expected keys, raising
            # JSONDecodeError/KeyError/TypeError — not just ScrapeForgeError. One subreddit's
            # failure must never abort the batch, so isolate it here and let the rest ingest.
            failed += 1
            log.warning("ingest-reddit: skipping r/%s — scrape failed: %s", source.subreddit, exc)
            continue
        for result in results:
            if result.status != "success" or result.article is None:
                continue
            article = result.article
            if not (article.content or "").strip():
                continue  # link post / empty self-post — no text for the summarizer
            score = article.metadata.get("score")
            if isinstance(score, int) and score < min_score:
                continue  # below the engagement floor
            if sink.seen(article.url):
                continue
            await sink.write(result)
            persisted += 1
    log.info(
        "ingest-reddit: persisted %d posts across %d subreddits (%d failed)",
        persisted,
        len(subreddits),
        failed,
    )
    return persisted
