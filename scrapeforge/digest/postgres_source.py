"""Load recent summarized articles from Postgres, relevance-ranked, for the digest.

The query is inlined here (NOT in repositories.py) per the seam rules. ``summary IS NOT NULL``
implies ``relevance IS NOT NULL`` (the summarizer writes both together), so a plain relevance-desc
order needs no NULLS-LAST handling. ``load_ranked_articles_sync`` is the async-in-sync bridge for
the (run-once) digest CLI.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.models import Article


async def load_ranked_articles(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    window_hours: int,
    limit: int,
) -> list[Article]:
    """Return up to *limit* summarized articles from the last *window_hours*, relevance-desc."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ArticleRow)
                    .where(ArticleRow.summary.is_not(None), ArticleRow.fetched_at >= cutoff)
                    .order_by(ArticleRow.relevance.desc(), ArticleRow.fetched_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    return [
        Article(
            url=row.url,
            title=row.title,
            content=row.content,
            author=row.author,
            publish_date=row.publish_date,
            metadata={
                "source_domain": row.domain,
                "bucket": row.bucket,
                "relevance": row.relevance,
                "summary": row.summary,
            },
        )
        for row in rows
    ]


def load_ranked_articles_sync(database_url: str, *, window_hours: int, limit: int) -> list[Article]:
    """Sync bridge for the digest CLI: build an engine, run the async loader, dispose."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    async def _run() -> list[Article]:
        engine = make_engine(database_url)
        try:
            return await load_ranked_articles(
                make_sessionmaker(engine), window_hours=window_hours, limit=limit
            )
        finally:
            await engine.dispose()

    return asyncio.run(_run())
