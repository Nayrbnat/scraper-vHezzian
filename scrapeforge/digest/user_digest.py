"""Assemble a per-user Digest from cosine-ranked articles (pure; no DB).

The articles arrive already ordered by the user's relevance score (from
``user_source.load_user_ranked_articles``), so this module preserves their order and wraps them in
a single "Top updates" section, attaching each article's SHARED 1-10 relevance + 5-bullet summary
(same summary every user sees) from ``article.metadata``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from scrapeforge.core.models import Article
from scrapeforge.digest.matcher import summarize
from scrapeforge.digest.models import Digest, DigestItem, DigestSection
from scrapeforge.digest.user_source import ActiveUser


def _relevance(article: Article) -> int | None:
    value = article.metadata.get("relevance")
    return value if isinstance(value, int) else None


def _item(article: Article) -> DigestItem:
    summary = article.metadata.get("summary")
    summary = summary if isinstance(summary, dict) else {}
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
        relevance=_relevance(article),
        reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
    )


def build_user_digest(
    user: ActiveUser, articles: list[Article], *, now: datetime | None = None
) -> Digest:
    """Wrap *articles* (already cosine-ordered) in one "Top updates" section for *user*."""
    now = now or datetime.now(UTC)
    items = [_item(a) for a in articles]  # preserve query order — do NOT re-sort
    sections = [DigestSection(key="top", heading="Top updates", items=items)] if items else []
    return Digest(
        subscriber_id=user.user_id,
        subscriber_name=user.name,
        subscriber_email=user.email,
        cadence="daily",
        period=now.date().isoformat(),
        generated_at=now,
        sections=sections,
    )
