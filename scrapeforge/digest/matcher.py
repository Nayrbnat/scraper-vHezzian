"""Build a personalized ``Digest`` from scraped ``Article``s (prototype: deterministic).

Matching is intentionally simple and offline for the prototype — case-insensitive
keyword/phrase matching of each subscriber preference against the article's title + content,
ranked by recency. A later phase swaps this for semantic relevance (pgvector) + LLM summaries
without changing the ``Digest`` contract.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from scrapeforge.core.models import Article
from scrapeforge.digest.models import (
    Digest,
    DigestItem,
    DigestPreferences,
    DigestSection,
    Subscriber,
)

# Section definitions in priority order: an article is placed in the FIRST section it matches,
# so a company hit outranks a theme hit which outranks a generic topic hit.
_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("portfolio", "Your portfolio companies", "portfolio_companies"),
    ("themes", "Investment themes", "investment_themes"),
    ("topics", "More in your topics", "news_topics"),
)


def _haystack(article: Article) -> str:
    return f"{article.title}\n{article.content}".lower()


def _matches(haystack: str, term: str) -> bool:
    """Whole-word/phrase, case-insensitive match (so 'AI' won't match 'mountain'/'ai_model')."""
    term = term.strip().lower()
    if not term:
        return False
    # Word-boundary lookarounds incl. underscore (so 'ai' doesn't match inside 'ai_model').
    return re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", haystack) is not None


def summarize(content: str, *, max_chars: int = 300) -> str:
    """Naive summary: the lead text, trimmed at a sentence boundary near *max_chars*."""
    text = " ".join(content.split())  # collapse whitespace
    if len(text) <= max_chars:
        return text
    window = text[: max_chars + 60]
    cut = window.rfind(". ", 0, max_chars + 1)
    if cut >= 60:  # prefer a sentence boundary if we found a reasonable one
        return window[: cut + 1]
    return text[:max_chars].rstrip() + "…"


def _matched_terms(haystack: str, terms: list[str]) -> list[str]:
    return [t for t in terms if _matches(haystack, t)]


def _to_item(article: Article, matched_on: list[str]) -> DigestItem:
    return DigestItem(
        title=article.title or "(untitled)",
        url=article.url,
        source=urlsplit(article.url).hostname or "",
        published=article.publish_date,
        summary=summarize(article.content),
        matched_on=matched_on,
    )


def _sort_key(article: Article) -> tuple[int, float]:
    # Most recent first; undated articles sort last (stable).
    if article.publish_date is None:
        return (0, 0.0)
    return (1, article.publish_date.timestamp())


def build_digest(
    subscriber: Subscriber,
    articles: list[Article],
    *,
    now: datetime | None = None,
) -> Digest:
    """Assemble a personalized digest for *subscriber* from *articles*.

    Each article is placed in the highest-priority section whose preference list it matches
    (portfolio > themes > topics); each section is capped at ``preferences.max_items_per_section``
    and ordered most-recent-first. An article matching nothing is dropped.
    """
    now = now or datetime.now(UTC)
    prefs: DigestPreferences = subscriber.preferences
    pref_terms = {
        "portfolio_companies": prefs.portfolio_companies,
        "investment_themes": prefs.investment_themes,
        "news_topics": prefs.news_topics,
    }

    buckets: dict[str, list[DigestItem]] = {key: [] for key, _, _ in _SECTIONS}
    for article in sorted(articles, key=_sort_key, reverse=True):
        haystack = _haystack(article)
        for key, _heading, pref_field in _SECTIONS:
            matched = _matched_terms(haystack, pref_terms[pref_field])
            if matched:
                buckets[key].append(_to_item(article, matched))
                break  # first matching section wins

    sections = [
        DigestSection(key=key, heading=heading, items=buckets[key][: prefs.max_items_per_section])
        for key, heading, _ in _SECTIONS
        if buckets[key]
    ]

    return Digest(
        subscriber_id=subscriber.id,
        subscriber_name=subscriber.name,
        subscriber_email=subscriber.email,
        cadence=prefs.cadence,
        period=now.date().isoformat(),
        generated_at=now,
        sections=sections,
    )
