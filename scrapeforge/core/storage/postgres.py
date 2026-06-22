"""PostgreSQL-backed ``ArticleSink`` for the serving plane (SPEC.md §3.18, W4).

``PostgresSink`` writes structured article data to the ``articles`` table using an
idempotent ``INSERT … ON CONFLICT (id) DO UPDATE`` UPSERT so the PK constraint is
never violated by duplicate scrape results.  The AUTHORITATIVE deduplication is
the database constraint; the in-process ``_seen_cache`` is a cheap best-effort
guard for the same Python process.

Responsibilities (SRP):
- Accept a ``ScrapeResult``, map the ``ArticleDTO`` to an ``ArticleRow``, UPSERT.
- Track written IDs in ``_seen_cache`` for fast in-process ``seen()`` checks.
- Merge article metadata with scrape provenance (driver_used, proxy_used, etc.)
  into the JSONB ``meta`` column so the downstream RAG pipeline has full context.

This file is the ONLY place that imports the PG-dialect UPSERT
(``sqlalchemy.dialects.postgresql.insert``); no other storage module does so.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.models import Article as ArticleDTO
from scrapeforge.core.models import ScrapeResult
from scrapeforge.core.storage.base import ArticleSink, url_id

# Mutable columns that the UPSERT may overwrite on a conflict.
# ``id`` is the PK and is excluded — it never changes.
# ``url`` is also excluded — the PK is derived from it, so it cannot change.
_UPSERT_UPDATE_COLUMNS = {
    "domain",
    "bucket",
    "title",
    "content",
    "author",
    "publish_date",
    "fetched_at",
    "raw_key",
    "meta",
}


class PostgresSink(ArticleSink):
    """Production ``ArticleSink`` backed by PostgreSQL (pgvector).

    Uses an idempotent UPSERT (``ON CONFLICT (id) DO UPDATE``) so writing the
    same URL multiple times is safe and always reflects the most-recently scraped
    data.  The ``_seen_cache`` is an in-process best-effort dedup guard; the
    authoritative dedup across processes / runs is the PK constraint.

    Dedup is URL-keyed only.  Unlike ``JsonlSink``, content-hash dedup
    (same content under a different URL) is intentionally NOT done here — it is
    deferred to the downstream post-processing / RAG layer.

    Args:
        session_factory: An ``async_sessionmaker[AsyncSession]`` (from
            ``core/db/session.make_sessionmaker``).  The caller owns the engine
            lifecycle; ``close()`` is therefore a no-op here.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._seen_cache: set[str] = set()

    # ------------------------------------------------------------------
    # ArticleSink interface
    # ------------------------------------------------------------------

    async def write(self, result: ScrapeResult) -> None:
        """UPSERT *result* into the ``articles`` table.

        Skips silently if:
        - ``result.status != 'success'``
        - ``result.article is None``

        Otherwise builds an ``ArticleRow`` dict, runs the PG UPSERT, and adds
        the URL id to ``_seen_cache``.
        """
        if result.status != "success" or result.article is None:
            return

        article: ArticleDTO = result.article
        doc_id = url_id(article.url)

        # --- Resolve domain --------------------------------------------------
        domain = article.metadata.get("source_domain") or (
            urllib.parse.urlsplit(article.url).hostname or ""
        )

        # --- Build provenance dict -------------------------------------------
        # Merge article.metadata with scrape-level provenance fields so the
        # downstream RAG pipeline has full context.  All four provenance keys are
        # always present; a None value is stored as JSON null in the JSONB column.
        provenance: dict[str, object] = {
            "driver_used": result.driver_used,
            "proxy_used": result.proxy_used,
            "challenge_solved": result.challenge_solved,
            "fetch_duration_ms": result.fetch_duration_ms,
        }
        # Start with article metadata; let provenance overwrite matching keys
        # (they are more authoritative at the result level).
        meta: dict[str, object] = {**article.metadata, **provenance}

        # --- Build row dict --------------------------------------------------
        row: dict[str, object] = {
            "id": doc_id,
            "url": article.url,
            "domain": domain,
            "bucket": article.metadata.get("bucket", ""),
            "title": article.title,
            "content": article.content,
            "author": article.author,
            "publish_date": article.publish_date,
            "fetched_at": datetime.now(UTC),
            "raw_key": article.metadata.get("raw_key"),
            "meta": meta,
        }

        # --- UPSERT ----------------------------------------------------------
        stmt = pg_insert(ArticleRow).values(**row)
        update_set = {col: getattr(stmt.excluded, col) for col in _UPSERT_UPDATE_COLUMNS}
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)

        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

        self._seen_cache.add(doc_id)

    def seen(self, url: str) -> bool:
        """Return ``True`` if *url* has been written by this sink instance.

        This is an in-process best-effort check populated by :meth:`write`.
        Cross-process / cross-run deduplication is guaranteed by the UPSERT PK
        constraint (``ON CONFLICT (id) DO UPDATE``), NOT by ``seen()``.  Do NOT
        perform a synchronous DB query here — keep the call cheap.

        NOTE — unlike ``JsonlSink``, this does NOT implement the ABC's durable
        resume-manifest behaviour: a fresh ``PostgresSink`` returns ``False`` for
        an already-persisted URL, so resume-after-restart will RE-SCRAPE rather
        than skip.  That is safe (the UPSERT dedups the write) and intentional —
        re-scraping is cheap and the transform worker relies on idempotent UPSERT,
        not on ``seen()``, for correctness.  Do not use ``PostgresSink.seen()`` as
        a crash-resume skip gate.
        """
        return url_id(url) in self._seen_cache

    async def close(self) -> None:
        """No-op.

        The ``session_factory`` / engine lifecycle is owned by the caller.
        This method exists for ``ArticleSink`` interface parity and is safe to
        call multiple times (idempotent).
        """
