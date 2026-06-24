"""Typer sub-app for run-once pipeline jobs (cron/deploy).

Mounted in the root CLI; invoked as ``scrapeforge pipeline <cmd>`` (the Render cron command).
Only ``asyncio.run`` appears here — the CLI is the sanctioned off-loop entry (Invariant #12).
"""

from __future__ import annotations

import asyncio
import sys

import typer

pipeline_app = typer.Typer(help="Run-once pipeline jobs for scheduled deployment.")


def _use_selector_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pipeline_app.command("init-db")
def init_db_cmd() -> None:
    """Prepare the database (pgvector + tables + columns). Idempotent — run once on first deploy."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine
    from scrapeforge.pipeline.jobs import init_db

    async def _run() -> None:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            await init_db(engine)
        finally:
            await engine.dispose()

    asyncio.run(_run())
    typer.echo("init-db: schema ready.")


@pipeline_app.command("ingest")
def ingest_cmd(
    limit: int = typer.Option(25, "--limit", "-l", help="Max posts per publication"),
    sector: str | None = typer.Option(None, "--sector", "-s", help="Only this sector"),
    max_pubs: int | None = typer.Option(None, "--max", "-m", help="Cap number of publications"),
    rss: bool = typer.Option(
        True,
        "--rss/--no-rss",
        help="Use RSS feeds (avoids the /api/v1 rate limits) instead of the JSON API.",
    ),
) -> None:
    """Scrape the curated Substacks straight into Postgres (no Redis/MinIO)."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.pipeline.jobs import ingest_publications
    from scrapeforge.scrapers.community.substack import SubstackScraper
    from scrapeforge.scrapers.community.substack_sources import select_sources

    sources = select_sources(sector=sector, limit=max_pubs)

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await ingest_publications(
                session_factory=make_sessionmaker(engine),
                scraper=SubstackScraper(),
                sources=sources,
                limit=limit,
                via_rss=rss,
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    mode = "RSS" if rss else "JSON API"
    typer.echo(f"ingest: persisted {n} articles from {len(sources)} publication(s) via {mode}.")


@pipeline_app.command("summarize")
def summarize_cmd() -> None:
    """Summarize + score all un-summarized articles once, then exit (run-once drain)."""
    _use_selector_loop()
    import logging

    from scrapeforge.core.llm.settings import SummarizerSettings

    settings = SummarizerSettings()
    if not settings.SUMMARY_API_KEY:
        logging.getLogger(__name__).warning(
            "SUMMARY_API_KEY empty — summarize skipped (set it to enable)."
        )
        typer.echo("summarize: skipped (no SUMMARY_API_KEY).")
        return

    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.migrations import ensure_summary_columns
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer
    from scrapeforge.worker.summarize_worker import run_summarize_worker

    async def _run() -> None:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            await ensure_summary_columns(engine)  # self-heal columns on an existing DB
            await run_summarize_worker(
                session_factory=make_sessionmaker(engine),
                summarizer=OpenAICompatibleSummarizer(settings),
                settings=settings,
            )
        finally:
            await engine.dispose()

    asyncio.run(_run())
    typer.echo("summarize: done.")
