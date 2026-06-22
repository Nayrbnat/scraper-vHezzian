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
