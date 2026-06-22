"""Deployment entry point for the SCRAPER worker (Phase 6).

Wires the real adapters (RedisQueue + MinioStore + ScrapeEngine) and runs the
``run_scraper_worker`` drain loop forever, polling the JOB queue. Stateless w.r.t. the
serving DB. Run via ``python -m scrapeforge.worker.run_scraper`` (see docker-compose).
"""

from __future__ import annotations

import asyncio
import sys

import redis.asyncio as aioredis

from scrapeforge.config.settings import Settings
from scrapeforge.core.engine import ScrapeEngine
from scrapeforge.core.objectstore.minio_store import MinioStore
from scrapeforge.core.queue.redis_queue import RedisQueue
from scrapeforge.worker.scraper_worker import run_scraper_worker

_POLL_INTERVAL_S = 2.0


async def main() -> None:
    if sys.platform == "win32":  # curl_cffi needs the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    settings = Settings()
    queue = RedisQueue(aioredis.from_url(settings.REDIS_URL), dlq_suffix=settings.DLQ_SUFFIX)
    store = MinioStore.from_settings(settings)
    await store.ensure_bucket()
    engine = ScrapeEngine()
    while True:  # poll: drain the JOB queue, then idle briefly
        await run_scraper_worker(queue=queue, store=store, engine=engine, settings=settings)
        await asyncio.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
