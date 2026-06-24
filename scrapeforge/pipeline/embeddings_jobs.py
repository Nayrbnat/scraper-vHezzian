"""Phase-3 multi-user embedding jobs (pure-async; injected Embedder).

Mirrors the summarize worker's shape. This module will hold ``embed_articles`` plus (added in
later tasks) ``embed_profiles``, ``score_users``, and ``seed_owner``. Queries/updates are inlined
here (not added to ``repositories.py``) per the seam rules. No raw SQL.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article
from scrapeforge.core.embeddings.base import Embedder

log = logging.getLogger(__name__)

_ARTICLE_TEXT_CHARS = 2000  # title + leading body fed to the embedder


async def embed_articles(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    batch_size: int,
) -> int:
    """Embed articles WHERE ``embedding IS NULL`` (newest first). Returns rows updated."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Article.id, Article.title, Article.content)
                .where(Article.embedding.is_(None))
                .order_by(Article.fetched_at.desc(), Article.id.desc())
                .limit(batch_size)
            )
        ).all()
    if not rows:
        return 0

    texts = [f"{title}\n\n{(content or '')[:_ARTICLE_TEXT_CHARS]}" for _id, title, content in rows]
    vectors = await embedder.embed(texts)

    updated = 0
    async with session_factory() as session:
        for (article_id, _title, _content), vector in zip(rows, vectors, strict=True):
            await session.execute(
                update(Article).where(Article.id == article_id).values(embedding=vector)
            )
            updated += 1
        await session.commit()
    log.info("embed_articles: embedded %d article(s)", updated)
    return updated
