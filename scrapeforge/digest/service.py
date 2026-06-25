"""Wire the digest prototype together: load subscriber → get articles → build → render → send."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scrapeforge.core.models import Article
from scrapeforge.digest.matcher import build_digest
from scrapeforge.digest.models import Digest, Subscriber
from scrapeforge.digest.render import RenderedEmail, render_email
from scrapeforge.digest.samples import sample_articles
from scrapeforge.digest.sender import EmailSender, PreviewEmailSender

log = logging.getLogger(__name__)


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
    - ``postgres``          → recent summarized articles from the DB, relevance-ranked
                             (window/limit from ``DigestSettings``); built into the
                             relevance digest by :func:`make_digest`.
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
    if source == "postgres":
        from scrapeforge.config.settings import Settings
        from scrapeforge.digest.postgres_source import load_ranked_articles_sync
        from scrapeforge.digest.settings import DigestSettings

        ds = DigestSettings()
        return load_ranked_articles_sync(
            Settings().DATABASE_URL, window_hours=ds.DIGEST_WINDOW_HOURS, limit=ds.DIGEST_TOP_N
        )
    raise ValueError(
        f"unknown article source {source!r} (use 'sample', 'jsonl:<path>', or 'postgres')"
    )


def make_digest(subscriber: Subscriber, source: str = "sample") -> tuple[Digest, RenderedEmail]:
    """Build + render a digest for *subscriber* from *source*. (No send.)"""
    articles = get_articles(source)
    if source == "postgres":
        from scrapeforge.digest.relevance import build_relevance_digest
        from scrapeforge.digest.settings import DigestSettings

        ds = DigestSettings()
        digest = build_relevance_digest(
            subscriber, articles, min_relevance=ds.DIGEST_RELEVANCE_FLOOR, limit=ds.DIGEST_TOP_N
        )
    else:
        digest = build_digest(subscriber, articles)
    return digest, render_email(digest)


def deliver(
    subscriber_path: Path | str,
    *,
    source: str = "sample",
    sender: EmailSender | None = None,
    to: str | None = None,
) -> Digest:
    """End-to-end: load → build → render → send. Defaults to the preview sender (no creds).

    The recipient is the subscriber's own email (the production model: each subscriber gets their
    own digest). Pass *to* (e.g. from ``DIGEST_TO``) to override it — handy for prototype testing
    so every send goes to one test inbox regardless of the seeded subscriber.
    """
    subscriber = load_subscriber(subscriber_path)
    digest, email = make_digest(subscriber, source)
    (sender or PreviewEmailSender()).send(to or subscriber.email, email)
    return digest


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    """Outcome counts for a per-user delivery run."""

    sent: int = 0
    skipped_empty: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return f"sent={self.sent} skipped_empty={self.skipped_empty} failed={self.failed}"


def deliver_all(*, source: str = "postgres", sender: EmailSender | None = None) -> DeliverySummary:
    """Send each active user their own relevance-ranked digest. Per-user failures are isolated:
    one user's bad render/send is logged and counted, never aborting the batch. Empty digests are
    skipped (no blank email). Defaults to the preview sender (no creds)."""
    if source != "postgres":
        raise ValueError(f"deliver_all only supports source='postgres', got {source!r}")

    from scrapeforge.config.settings import Settings
    from scrapeforge.digest.settings import DigestSettings
    from scrapeforge.digest.user_digest import build_user_digest
    from scrapeforge.digest.user_source import load_all_sync

    ds = DigestSettings()
    batches = load_all_sync(
        Settings().DATABASE_URL,
        window_hours=ds.DIGEST_USER_WINDOW_HOURS,
        score_floor=ds.DIGEST_USER_SCORE_FLOOR,
        limit=ds.DIGEST_USER_TOP_N,
    )
    sender = sender or PreviewEmailSender()

    sent = skipped = failed = 0
    for user, articles in batches:
        try:
            if not articles:
                skipped += 1
                continue
            digest = build_user_digest(user, articles)
            sender.send(user.email, render_email(digest))
            sent += 1
        except Exception:  # noqa: BLE001 — isolate one user's failure from the rest of the batch
            log.exception("deliver_all: delivery failed for user %s", user.user_id)
            failed += 1
    return DeliverySummary(sent=sent, skipped_empty=skipped, failed=failed)
