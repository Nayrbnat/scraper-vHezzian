"""Batch summarizer worker (Phase 2): score + summarize articles WHERE summary IS NULL.

Reads a batch of un-summarized articles, calls the injected ``Summarizer``, and writes the
``relevance`` (int) + ``summary`` (JSONB) columns. Idempotent (the NULL gate), rate-limit
paced, and resilient (a per-article parse error skips that row; a rate-limit stops the run).
The query/update are inlined here (not added to ``repositories.py``) per the seam rules.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article
from scrapeforge.core.llm.base import Summarizer
from scrapeforge.core.llm.exceptions import LLMError, LLMRateLimitError

log = logging.getLogger(__name__)


async def summarize_pending(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    summarizer: Summarizer,
    settings,
    refresh_limit: int | None = None,
) -> int:
    """Summarize one batch and return the count persisted.

    Default: newest ``SUMMARY_BATCH_SIZE`` articles WHERE ``summary IS NULL`` (idempotent drain).
    When *refresh_limit* is set: the newest *refresh_limit* articles regardless of summary state —
    re-summarizing (overwriting) them, used to apply a new prompt/focus to existing rows.
    """
    async with session_factory() as session:
        stmt = select(Article).order_by(Article.fetched_at.desc(), Article.id.desc())
        if refresh_limit is not None:
            stmt = stmt.limit(refresh_limit)
        else:
            stmt = stmt.where(Article.summary.is_(None)).limit(settings.SUMMARY_BATCH_SIZE)
        rows = (await session.execute(stmt)).scalars().all()
        pending = [(r.id, r.title, r.content, r.publish_date) for r in rows]

    count = 0
    for article_id, title, content, published in pending:
        try:
            result = await summarizer.summarize(
                title=title,
                content=content,
                published=published,
                portfolio=settings.portfolio(),
                interests=settings.interests(),
            )
        except LLMRateLimitError:
            log.warning("summarize: rate-limited; stopping run after %d persisted", count)
            break
        except LLMError as exc:
            log.warning("summarize: skipping %s: %s", article_id, exc)
            continue

        async with session_factory() as session:
            await session.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(
                    relevance=result.relevance,
                    summary={
                        "bullets": result.bullets,
                        "scores": result.scores,
                        "reason": result.reason,
                        "model": result.model,
                        "generated_at": datetime.now(UTC).isoformat(),
                    },
                )
            )
            await session.commit()
        count += 1
        if settings.SUMMARY_INTER_REQUEST_DELAY:
            await asyncio.sleep(settings.SUMMARY_INTER_REQUEST_DELAY)

    return count


async def run_summarize_worker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    summarizer: Summarizer,
    settings,
) -> None:
    """Drain all pending articles in successive batches until none remain."""
    while (
        await summarize_pending(
            session_factory=session_factory, summarizer=summarizer, settings=settings
        )
        > 0
    ):
        pass
