"""Deployment entry point for the SUMMARIZER worker (Phase 2).

Builds the OpenAI-compatible summarizer + a DB session factory and drains un-summarized
articles in a poll loop. Empty SUMMARY_API_KEY => idle (no crash-loop, no spend). Run via
``python -m scrapeforge.worker.run_summarize``.
"""

from __future__ import annotations

import asyncio
import logging

from scrapeforge.config.settings import Settings
from scrapeforge.core.db.migrations import ensure_summary_columns
from scrapeforge.core.db.session import make_engine, make_sessionmaker
from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer
from scrapeforge.core.llm.settings import SummarizerSettings
from scrapeforge.worker.summarize_worker import run_summarize_worker

log = logging.getLogger(__name__)
_POLL_INTERVAL_S = 60.0


async def main() -> None:
    summarizer_settings = SummarizerSettings()

    # Key check FIRST — a keyless summarizer idles cleanly without touching the DB, so a
    # misconfigured-but-keyless container never crash-loops on a schema/connectivity error.
    if not summarizer_settings.SUMMARY_API_KEY:
        log.warning("SUMMARY_API_KEY is empty — summarizer idle (set it in .env to enable).")
        while True:  # noqa: ASYNC110
            await asyncio.sleep(_POLL_INTERVAL_S)

    engine = make_engine(Settings().DATABASE_URL)
    await ensure_summary_columns(engine)  # idempotent: self-heal the schema on existing DBs
    session_factory = make_sessionmaker(engine)

    summarizer = OpenAICompatibleSummarizer(summarizer_settings)
    while True:
        await run_summarize_worker(
            session_factory=session_factory, summarizer=summarizer, settings=summarizer_settings
        )
        await asyncio.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
