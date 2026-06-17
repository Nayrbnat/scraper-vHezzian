"""Public-bucket Typer sub-app (SPEC.md §5.2, Invariant #16).

Adding this sub-app is a new file in the public package — the root ``cli.py``
mounts it via discovery but is *not* edited here.  This is the seam that keeps
parallel agents conflict-free.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from scrapeforge.core.engine import ScrapeEngine
from scrapeforge.core.storage.jsonl import JsonlSink

public_app = typer.Typer(help="Bucket 3 — public news (generic curl_cffi scraper)")


@public_app.command("scrape")
def scrape_public(
    source: str = typer.Argument(..., help="URL to scrape"),
    output: Path = typer.Option(Path("./output"), "--output", "-o", help="Output base path"),  # noqa: B008
    proxy: str | None = typer.Option(None, "--proxy", help="Proxy URL (optional)"),
) -> None:
    """Scrape a single public URL and write the result to a JSONL sink."""
    sink = JsonlSink(output)
    engine = ScrapeEngine(sink=sink)

    result = asyncio.run(engine.scrape(source))

    title = result.article.title if result.article else "<no title>"
    driver = result.driver_used
    status = result.status

    typer.echo(f"status={status} driver={driver} title={title!r}")
