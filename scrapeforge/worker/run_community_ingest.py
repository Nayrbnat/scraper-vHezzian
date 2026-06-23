"""Deployment entry point for the COMMUNITY-INGEST worker (Phase 1).

Wires the real adapters (RedisQueue + MinioStore + async DB session factory) and runs the
``run_community_ingest_worker`` drain loop forever, polling the INGEST queue. Persists
community-publication articles directly (Invariant #18 carve-out). Run via
``python -m scrapeforge.worker.run_community_ingest``.
"""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from scrapeforge.config.settings import Settings
from scrapeforge.core.db.session import make_engine, make_sessionmaker
from scrapeforge.core.objectstore.minio_store import MinioStore
from scrapeforge.core.queue.redis_queue import RedisQueue
from scrapeforge.worker.community_ingest_worker import run_community_ingest_worker

_POLL_INTERVAL_S = 2.0


async def main() -> None:
    settings = Settings()
    queue = RedisQueue(aioredis.from_url(settings.REDIS_URL), dlq_suffix=settings.DLQ_SUFFIX)
    store = MinioStore.from_settings(settings)
    session_factory = make_sessionmaker(make_engine(settings.DATABASE_URL))
    while True:  # poll: drain the INGEST queue, then idle briefly
        await run_community_ingest_worker(
            queue=queue, store=store, session_factory=session_factory, settings=settings
        )
        await asyncio.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
