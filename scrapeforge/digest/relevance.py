"""Assemble a relevance-ranked Digest from scored articles (pure; no DB).

The Postgres source (digest.postgres_source) stashes each article's ``relevance`` and its
``summary`` JSONB (``bullets``/``reason``) into ``article.metadata``; this builder reads those,
filters to a floor, sorts by relevance (recency tiebreak), caps at a limit, and wraps the result
in a single "Top updates" section.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from scrapeforge.core.models import Article
from scrapeforge.digest.matcher import summarize
from scrapeforge.digest.models import Digest, DigestItem, DigestSection, Subscriber


def _relevance(article: Article) -> int:
    value = article.metadata.get("relevance")
    return value if isinstance(value, int) else 0


def _item(article: Article) -> DigestItem:
    summary = article.metadata.get("summary") or {}
    raw_bullets = summary.get("bullets") or []
    bullets = [b.strip() for b in raw_bullets if isinstance(b, str) and b.strip()]
    reason = summary.get("reason")
    return DigestItem(
        title=article.title or "(untitled)",
        url=article.url,
        source=urlsplit(article.url).hostname or "",
        published=article.publish_date,
        summary=summarize(article.content),  # lead-text fallback
        bullets=bullets,
        relevance=_relevance(article) or None,
        reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
    )


def _sort_key(article: Article) -> tuple[int, float]:
    ts = article.publish_date.timestamp() if article.publish_date else 0.0
    return (_relevance(article), ts)


def build_relevance_digest(
    subscriber: Subscriber,
    articles: list[Article],
    *,
    min_relevance: int = 5,
    limit: int = 10,
    now: datetime | None = None,
) -> Digest:
    """Build a single "Top updates" section ranked by relevance (>= floor, capped at *limit*)."""
    now = now or datetime.now(UTC)
    eligible = [a for a in articles if _relevance(a) >= min_relevance]
    eligible.sort(key=_sort_key, reverse=True)
    items = [_item(a) for a in eligible[:limit]]
    sections = [DigestSection(key="top", heading="Top updates", items=items)] if items else []
    return Digest(
        subscriber_id=subscriber.id,
        subscriber_name=subscriber.name,
        subscriber_email=subscriber.email,
        cadence=subscriber.preferences.cadence,
        period=now.date().isoformat(),
        generated_at=now,
        sections=sections,
    )
