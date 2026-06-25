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


@pipeline_app.command("prune")
def prune_cmd(
    days: int | None = typer.Option(None, "--days", help="Override retention window (days)."),
    max_articles: int | None = typer.Option(None, "--max", help="Override the hard article cap."),
) -> None:
    """Delete old / irrelevant articles to keep storage under the DB cap (oldest-first)."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.pipeline.retention import RetentionSettings, prune_articles

    rs = RetentionSettings()

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await prune_articles(
                session_factory=make_sessionmaker(engine),
                retention_days=days if days is not None else rs.RETENTION_DAYS,
                max_articles=max_articles
                if max_articles is not None
                else rs.RETENTION_MAX_ARTICLES,
                relevance_floor=rs.RETENTION_RELEVANCE_FLOOR,
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"prune: deleted {n} article(s).")


@pipeline_app.command("summarize")
def summarize_cmd(
    refresh: int = typer.Option(
        0,
        "--refresh",
        help="Re-summarize the N most recent articles (overwrite) to apply a new prompt/focus, "
        "instead of draining only un-summarized ones.",
    ),
) -> None:
    """Summarize + score articles once, then exit (run-once). Default: drain un-summarized."""
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
    from scrapeforge.worker.summarize_worker import run_summarize_worker, summarize_pending

    async def _run() -> None:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            await ensure_summary_columns(engine)  # self-heal columns on an existing DB
            session_factory = make_sessionmaker(engine)
            summarizer = OpenAICompatibleSummarizer(settings)
            if refresh > 0:
                n = await summarize_pending(
                    session_factory=session_factory,
                    summarizer=summarizer,
                    settings=settings,
                    refresh_limit=refresh,
                )
                typer.echo(f"summarize: refreshed {n} recent article(s).")
            else:
                await run_summarize_worker(
                    session_factory=session_factory, summarizer=summarizer, settings=settings
                )
                typer.echo("summarize: done.")
        finally:
            await engine.dispose()

    asyncio.run(_run())


@pipeline_app.command("seed-owner")
def seed_owner_cmd() -> None:
    """Upsert the owner profile from SUMMARY_PORTFOLIO/INTERESTS/FOCUS (no API key needed)."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.core.llm.settings import SummarizerSettings
    from scrapeforge.pipeline.embeddings_jobs import seed_owner

    async def _run() -> None:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            await seed_owner(
                session_factory=make_sessionmaker(engine), settings=SummarizerSettings()
            )
        finally:
            await engine.dispose()

    asyncio.run(_run())
    typer.echo("seed-owner: owner profile upserted.")


def _embedder_or_skip(action: str):
    """Build the configured embedder, or return (None, None) and echo a skip if no key is set."""
    import logging

    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    settings = EmbedderSettings()
    if not settings.EMBED_API_KEY:
        logging.getLogger(__name__).warning("EMBED_API_KEY empty — %s skipped.", action)
        typer.echo(f"{action}: skipped (no EMBED_API_KEY).")
        return None, None
    return make_embedder(settings), settings


@pipeline_app.command("embed-articles")
def embed_articles_cmd() -> None:
    """Embed articles WHERE embedding IS NULL (idempotent). Skips if no EMBED_API_KEY."""
    _use_selector_loop()
    embedder, settings = _embedder_or_skip("embed-articles")
    if embedder is None:
        return

    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await embed_articles(
                session_factory=make_sessionmaker(engine),
                embedder=embedder,
                batch_size=settings.EMBED_BATCH_SIZE,
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"embed-articles: embedded {n} article(s).")


@pipeline_app.command("embed-profiles")
def embed_profiles_cmd() -> None:
    """Embed changed user profiles (source-hash gate). Skips if no EMBED_API_KEY."""
    _use_selector_loop()
    embedder, _settings = _embedder_or_skip("embed-profiles")
    if embedder is None:
        return

    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.pipeline.embeddings_jobs import embed_profiles

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await embed_profiles(
                session_factory=make_sessionmaker(engine), embedder=embedder
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"embed-profiles: (re-)embedded {n} profile(s).")


@pipeline_app.command("score-users")
def score_users_cmd() -> None:
    """Score recent articles per user via pgvector cosine similarity (no API key needed)."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.core.embeddings.settings import EmbedderSettings
    from scrapeforge.pipeline.embeddings_jobs import score_users

    settings = EmbedderSettings()

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await score_users(
                session_factory=make_sessionmaker(engine),
                window_days=settings.EMBED_SCORE_WINDOW_DAYS,
                top_k=settings.EMBED_TOP_K,
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"score-users: wrote {n} (user, article) score(s).")
