"""Append-only JSONL + manifest storage backend (SPEC.md §3.18).

``JsonlSink`` writes one flattened JSON object per article line to
``<output>.jsonl`` and maintains a ``<output>.manifest`` sidecar file so batch
jobs can resume safely after a crash.

Layout::

    <output>.jsonl        # one article per line: {id, url, title, content, …}
    <output>.manifest     # newline-delimited url_id() values (completed URLs)

Crash-safety order (write THEN manifest):
    1. The JSONL ``async with aiofiles.open(...)`` block closes the file
       (flushing to the OS buffer) before the manifest block opens.
    2. Append the url_id to the manifest file.
    3. Update the in-memory sets.

Note: "closed before manifest" means the data reaches the OS write buffer, not
that it is fsync-ed to stable storage.  Power loss between steps 1 and 2 may
still result in the article being written a second time on the next run; this is
acceptable because content-hash deduplication removes the duplicate in
post-processing.  If the process dies after step 2, ``seen()`` returns ``True``
on resume and the URL is skipped entirely.

Content-hash deduplication (``_seen_content``) is intra-run only: the set
starts empty on each new ``JsonlSink`` instance.  Same-content / different-URL
duplicates that appear across separate runs are removed in post-processing, not
here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import aiofiles

from scrapeforge.core.models import ScrapeResult
from scrapeforge.core.storage.base import ArticleSink, url_id


class JsonlSink(ArticleSink):
    """File-backed ``ArticleSink`` for local / CLI runs.

    Args:
        output: Base path for the output files.  The ``.jsonl`` extension is
            forced on the data file; ``.manifest`` on the sidecar.

    Attributes:
        path:           Resolved ``<output>.jsonl`` path.
        manifest_path:  Resolved ``<output>.manifest`` path.
    """

    def __init__(self, output: Path) -> None:
        self.path = output.with_suffix(".jsonl")
        self.manifest_path = output.with_suffix(".manifest")
        self._seen_urls: set[str] = self._load_manifest()
        self._seen_content: set[str] = set()

    # ------------------------------------------------------------------
    # ArticleSink interface
    # ------------------------------------------------------------------

    def seen(self, url: str) -> bool:
        """Return ``True`` if *url*'s ``url_id`` appears in the resume manifest."""
        return url_id(url) in self._seen_urls

    async def write(self, result: ScrapeResult) -> None:
        """Persist a successful scrape result.

        Skips if:
        - ``result.status != 'success'``
        - ``result.article is None``
        - The article's content hash has already been emitted (content dedup).
        """
        if result.status != "success" or result.article is None:
            return

        article = result.article
        content_hash = hashlib.sha256(article.content.encode()).hexdigest()
        if content_hash in self._seen_content:
            return

        doc_id = url_id(article.url)

        # Serialise publish_date as ISO string or None.
        pub_date = article.publish_date.isoformat() if article.publish_date is not None else None

        record = {
            "id": doc_id,
            "url": article.url,
            "title": article.title,
            "content": article.content,
            "author": article.author,
            "publish_date": pub_date,
            "metadata": article.metadata,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"

        # Step 1: write the JSONL line; the context manager closes (and OS-flushes)
        # the file before the manifest block opens (crash-safe ordering).
        async with aiofiles.open(self.path, "a", encoding="utf-8") as fh:
            await fh.write(line)

        # Step 2: append the url_id to the manifest.
        async with aiofiles.open(self.manifest_path, "a", encoding="utf-8") as mh:
            await mh.write(doc_id + "\n")

        # Step 3: update in-memory sets.
        self._seen_urls.add(doc_id)
        self._seen_content.add(content_hash)

    async def close(self) -> None:
        """No-op for file sinks (each write opens+closes its own handle)."""

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_manifest(self) -> set[str]:
        """Read the manifest file and return the set of completed url_ids.

        Uses a synchronous read at init time (no event loop required yet).
        Returns an empty set if the manifest does not exist.
        """
        if not self.manifest_path.exists():
            return set()
        text = self.manifest_path.read_text(encoding="utf-8")
        return {line.strip() for line in text.splitlines() if line.strip()}
