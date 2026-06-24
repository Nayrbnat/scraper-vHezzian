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
