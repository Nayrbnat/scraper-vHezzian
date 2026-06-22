"""Wire the digest prototype together: load subscriber → get articles → build → render → send."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from scrapeforge.core.models import Article
from scrapeforge.digest.matcher import build_digest
from scrapeforge.digest.models import Digest, Subscriber
from scrapeforge.digest.render import RenderedEmail, render_email
from scrapeforge.digest.samples import sample_articles
from scrapeforge.digest.sender import EmailSender, PreviewEmailSender


def load_subscriber(path: Path | str) -> Subscriber:
    """Load a seeded individual from JSON (the prototype stand-in for a users table)."""
    return Subscriber.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _article_from_jsonl(row: dict) -> Article:
    pub = row.get("publish_date")
    return Article(
        url=row["url"],
        title=row.get("title") or "",
        content=row.get("content") or "",
        author=row.get("author"),
        publish_date=datetime.fromisoformat(pub) if pub else None,
        metadata=row.get("metadata") or {},
    )


def get_articles(source: str) -> list[Article]:
    """Resolve the article source.

    - ``sample``            → the bundled sample corpus (standalone prototype).
    - ``jsonl:<path>``      → read a JsonlSink ``.jsonl`` produced by a real scrape.
    """
    if source == "sample":
        return sample_articles()
    if source.startswith("jsonl:"):
        path = Path(source.split(":", 1)[1])
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [_article_from_jsonl(r) for r in rows]
    raise ValueError(f"unknown article source {source!r} (use 'sample' or 'jsonl:<path>')")


def make_digest(subscriber: Subscriber, source: str = "sample") -> tuple[Digest, RenderedEmail]:
    """Build + render a digest for *subscriber* from *source*. (No send.)"""
    digest = build_digest(subscriber, get_articles(source))
    return digest, render_email(digest)


def deliver(
    subscriber_path: Path | str,
    *,
    source: str = "sample",
    sender: EmailSender | None = None,
) -> Digest:
    """End-to-end: load → build → render → send. Defaults to the preview sender (no creds)."""
    subscriber = load_subscriber(subscriber_path)
    digest, email = make_digest(subscriber, source)
    (sender or PreviewEmailSender()).send(subscriber.email, email)
    return digest
