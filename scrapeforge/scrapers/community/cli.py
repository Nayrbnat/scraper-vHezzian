"""Community-bucket Typer sub-app (Bucket 2 — SPEC.md Invariant #16).

Adding this sub-app is one new file in the community package — the root
``cli.py`` mounts it via discovery but is *not* edited here.

Only ``asyncio.run()`` appears here — the CLI entry point is not inside an
event loop, so this is the one sanctioned location (CLAUDE.md Invariant #12).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from scrapeforge.core.storage.jsonl import JsonlSink

community_app = typer.Typer(help="Bucket 2 — community/foreign sites")


def _use_selector_loop() -> None:
    """On Windows, curl_cffi requires the selector event-loop (not Proactor)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@community_app.command("scrape")
def scrape_community(
    platform: str = typer.Argument(..., help="Platform to scrape (e.g. 'reddit')"),
    target: str = typer.Argument(..., help="Target (e.g. subreddit name for Reddit)"),
    limit: int = typer.Option(25, "--limit", "-l", help="Maximum articles to fetch"),
    output: Path = typer.Option(  # noqa: B008
        Path("./output"),
        "--output",
        "-o",
        help="Output base path (JSONL + manifest written here)",
    ),
) -> None:
    """Scrape a community-bucket platform and write results to a JSONL sink."""
    _use_selector_loop()

    if platform == "reddit":
        from scrapeforge.scrapers.community.reddit import RedditScraper

        scraper = RedditScraper()

        async def _run() -> int:
            results = await scraper.scrape_subreddit(target, limit=limit)
            sink = JsonlSink(output)
            n = 0
            for result in results:
                if result.status == "success":
                    await sink.write(result)
                    n += 1
            await sink.close()
            return n

        success_count = asyncio.run(_run())
        typer.echo(f"Scraped {success_count} articles from r/{target} → {output}.jsonl")

    elif platform == "substack":
        from scrapeforge.scrapers.community.substack import SubstackScraper

        scraper_ss = SubstackScraper()

        async def _run_ss() -> int:
            results = await scraper_ss.scrape_publication(target, limit=limit)
            sink = JsonlSink(output)
            n = 0
            for result in results:
                if result.status == "success":
                    await sink.write(result)
                    n += 1
            await sink.close()
            return n

        success_count = asyncio.run(_run_ss())
        typer.echo(f"Scraped {success_count} articles from {target} → {output}.jsonl")

    else:
        typer.echo(f"Unknown platform: {platform!r}. Supported: reddit, substack", err=True)
        raise typer.Exit(code=1)


@community_app.command("scrape-substacks")
def scrape_substacks(
    sector: str | None = typer.Option(
        None, "--sector", "-s", help="Only scrape this sector (see --list for the labels)"
    ),
    limit: int = typer.Option(10, "--limit", "-l", help="Max posts to fetch per publication"),
    max_pubs: int | None = typer.Option(
        None, "--max", "-m", help="Cap the number of publications scraped"
    ),
    output: Path = typer.Option(  # noqa: B008
        Path("./output"),
        "--output",
        "-o",
        help="Output base path (JSONL written here)",
    ),
    list_only: bool = typer.Option(
        False, "--list", help="List the selected publications and exit (no scraping)"
    ),
) -> None:
    """Scrape the curated investing-Substack list via the publication archive API.

    Drives ``SubstackScraper.scrape_publication`` (archive discovery + per-post
    fetch, public-only) over the selected publications and writes every successful
    article to a JSONL sink — the same on-demand path as ``community scrape``.
    """
    from scrapeforge.scrapers.community.substack_sources import select_sources

    selected = select_sources(sector=sector, limit=max_pubs)
    if not selected:
        typer.echo(f"No curated publications match --sector {sector!r}.", err=True)
        raise typer.Exit(code=1)

    if list_only:
        for s in selected:
            flag = " [paid-leaning]" if s.paywall else ""
            typer.echo(f"{s.sector:22s} {s.name:30s} {s.url}{flag}")
        typer.echo(f"\n{len(selected)} publication(s) selected (list-only, nothing scraped).")
        return

    _use_selector_loop()

    from scrapeforge.scrapers.community.substack import SubstackScraper

    scraper = SubstackScraper()

    async def _run() -> tuple[int, int]:
        sink = JsonlSink(output)
        articles = 0
        scraped_pubs = 0
        try:
            for s in selected:
                results = await scraper.scrape_publication(s.base, limit=limit)
                wrote = 0
                for result in results:
                    if result.status == "success":
                        await sink.write(result)
                        wrote += 1
                articles += wrote
                scraped_pubs += 1
                typer.echo(f"  {s.name:30s} {wrote} article(s)")
        finally:
            await sink.close()
        return scraped_pubs, articles

    pubs, total = asyncio.run(_run())
    typer.echo(f"Scraped {total} articles from {pubs} publication(s) → {output}.jsonl")


@community_app.command("seed-substacks")
def seed_substacks(
    limit: int = typer.Option(
        25, "--limit", "-l", help="Per-source post cap stored on each Source"
    ),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Override DATABASE_URL (defaults to Settings)"
    ),
    enabled: bool = typer.Option(
        True, "--enabled/--disabled", help="Seed sources as scheduler-enabled or not"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the curated list without touching the database"
    ),
) -> None:
    """Seed the curated investing-Substack list into the ``sources`` table (idempotent).

    The scheduler then routes these community publications to the INGEST queue, where the
    community-ingest worker scrapes each on its daily tick.
    """
    from scrapeforge.scrapers.community.substack_sources import (
        SUBSTACK_INVESTING_SOURCES,
        seed_sources,
    )

    if dry_run:
        for s in SUBSTACK_INVESTING_SOURCES:
            flag = " [paid-leaning]" if s.paywall else ""
            typer.echo(f"{s.sector:22s} {s.name:30s} {s.url}{flag}")
        typer.echo(
            f"\n{len(SUBSTACK_INVESTING_SOURCES)} curated sources (dry-run, nothing written)."
        )
        return

    _use_selector_loop()

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    async def _run() -> int:
        engine = make_engine(database_url)
        session_factory = make_sessionmaker(engine)
        try:
            async with session_factory() as session:
                return await seed_sources(session, limit=limit, enabled=enabled)
        finally:
            await engine.dispose()

    count = asyncio.run(_run())
    state = "enabled" if enabled else "disabled"
    typer.echo(f"Seeded {count} Substack sources ({state}, limit={limit}) into the sources table.")
