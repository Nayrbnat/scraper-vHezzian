"""Deployment entry point for the SCHEDULER (Phase 6).

Periodically calls ``enqueue_due_sources`` to enqueue recurring scrapes for every enabled
``Source`` ("continuously populate"). Run via ``python -m scrapeforge.worker.run_scheduler``.
"""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from scrapeforge.config.settings import Settings
from scrapeforge.core.db.session import make_engine, make_sessionmaker
from scrapeforge.core.queue.redis_queue import RedisQueue
from scrapeforge.worker.scheduler import enqueue_due_sources

_TICK_INTERVAL_S = 300.0  # enqueue due sources every 5 minutes


async def main() -> None:
    settings = Settings()
    queue = RedisQueue(aioredis.from_url(settings.REDIS_URL), dlq_suffix=settings.DLQ_SUFFIX)
    session_factory = make_sessionmaker(make_engine(settings.DATABASE_URL))
    while True:
        await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)
        await asyncio.sleep(_TICK_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
