"""Storage seam: ``ArticleSink`` ABC and the shared ``url_id`` helper (SPEC.md §3.18).

Three responsibilities, three files — SRP (SPEC.md §3.18 note):
- ``base.py``     — interface + shared helper (this file).
- ``jsonl.py``    — ``JsonlSink``: file I/O + resume manifest (local/CLI).
- ``postgres.py`` — ``PostgresSink``: async UPSERT via ``core/db/`` (serving plane).

The ``ArticleSink`` ABC is the boundary to the downstream LLM/RAG pipeline; the
engine, scrapers, and workers use it without caring which backend is wired in.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from scrapeforge.core.models import ScrapeResult


def url_id(url: str) -> str:
    """Return the stable document id for *url*: SHA-256 hex-digest of the URL bytes.

    Used as a deduplication key in every ``ArticleSink`` backend.

    >>> url_id('https://example.com')  # doctest: +ELLIPSIS
    '...'
    """
    return hashlib.sha256(url.encode()).hexdigest()


class ArticleSink(ABC):
    """Abstract base class for all article-persistence backends.

    Invariants (SPEC.md §3.18):
    - ``write()`` is idempotent per URL: re-writing the same URL (or content)
      is silently skipped.
    - ``seen()`` reflects the *resume manifest*: batch jobs can skip completed
      URLs after a crash by calling ``seen(url)`` before scraping.
    - ``close()`` releases any open file handles or DB connections.
    """

    @abstractmethod
    async def write(self, result: ScrapeResult) -> None:
        """Persist *result* if it is a new, successful article.

        Implementations must:
        1. Skip if ``result.status != 'success'`` or ``result.article is None``.
        2. Skip if content has already been emitted (content-hash dedup).
        3. Persist the article atomically and update in-memory state so
           subsequent ``seen()`` calls return ``True``.
        """

    @abstractmethod
    def seen(self, url: str) -> bool:
        """Return ``True`` if *url* has already been persisted.

        Must be safe to call before the event loop is running (sync).
        """

    @abstractmethod
    async def close(self) -> None:
        """Release resources (file handles, DB sessions, …).

        Must be idempotent (safe to call multiple times).
        """
