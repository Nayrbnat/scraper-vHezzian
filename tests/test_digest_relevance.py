"""Pure tests for build_relevance_digest (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

from scrapeforge.core.models import Article
from scrapeforge.digest.models import DigestPreferences, Subscriber


def _sub() -> Subscriber:
    return Subscriber(
        id="dee", name="Dee", email="dee@example.com", preferences=DigestPreferences()
    )


def _article(url: str, title: str, *, relevance: int, bullets=None, reason="r", days_ago: int = 0):
    return Article(
        url=url,
        title=title,
        content="Body text long enough to summarize for a fallback blurb.",
        publish_date=datetime(2026, 6, 24, tzinfo=UTC).replace(day=24 - days_ago),
        metadata={
            "source_domain": "e.com",
            "bucket": "community",
            "relevance": relevance,
            "summary": {"bullets": bullets or [f"{title} b1", f"{title} b2"], "reason": reason},
        },
    )


def test_filters_below_floor_and_sorts_desc() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    arts = [
        _article("https://e.com/1", "Low", relevance=3),
        _article("https://e.com/2", "High", relevance=9),
        _article("https://e.com/3", "Mid", relevance=6),
    ]
    digest = build_relevance_digest(_sub(), arts, min_relevance=5, limit=10)
    assert len(digest.sections) == 1 and digest.sections[0].key == "top"
    titles = [i.title for i in digest.sections[0].items]
    assert titles == ["High", "Mid"]  # Low (<5) dropped; sorted desc
    top = digest.sections[0].items[0]
    assert top.relevance == 9 and top.bullets == ["High b1", "High b2"] and top.reason == "r"


def test_caps_at_limit() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    arts = [_article(f"https://e.com/{i}", f"A{i}", relevance=9 - (i % 3)) for i in range(8)]
    digest = build_relevance_digest(_sub(), arts, min_relevance=1, limit=3)
    assert len(digest.sections[0].items) == 3


def test_empty_when_nothing_clears_floor() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    arts = [_article("https://e.com/1", "Low", relevance=2)]
    digest = build_relevance_digest(_sub(), arts, min_relevance=5, limit=10)
    assert digest.sections == [] and digest.is_empty


def test_lead_text_fallback_summary_is_set() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    digest = build_relevance_digest(
        _sub(), [_article("https://e.com/1", "X", relevance=8)], min_relevance=5, limit=10
    )
    item = digest.sections[0].items[0]
    assert item.summary  # non-empty lead-text fallback (used if a client ignores bullets)
    assert item.source == "e.com"


def test_recency_tiebreak_newer_article_first() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    arts = [
        _article("https://e.com/old", "Older", relevance=7, days_ago=5),
        _article("https://e.com/new", "Newer", relevance=7, days_ago=0),
    ]
    digest = build_relevance_digest(_sub(), arts, min_relevance=5, limit=10)
    titles = [i.title for i in digest.sections[0].items]
    assert titles == ["Newer", "Older"]  # same relevance; recency tiebreak puts newer first


def test_degenerate_metadata_no_summary_key() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    # summary is a non-dict (malformed JSONB stored as a bare string) — must not raise
    art_bad_summary = Article(
        url="https://e.com/bad",
        title="Bad summary",
        content="Body text long enough to produce a non-empty lead-text fallback summary.",
        metadata={
            "source_domain": "e.com",
            "bucket": "community",
            "relevance": 7,
            "summary": "this-is-a-string-not-a-dict",
        },
    )
    # summary key absent entirely
    art_no_summary = Article(
        url="https://e.com/1",
        title="No summary",
        content="Body text long enough to produce a non-empty lead-text fallback summary.",
        metadata={"source_domain": "e.com", "bucket": "community", "relevance": 7},
    )
    for art in (art_bad_summary, art_no_summary):
        digest = build_relevance_digest(_sub(), [art], min_relevance=5, limit=10)
        item = digest.sections[0].items[0]
        assert item.bullets == []
        assert item.reason is None
        assert item.relevance == 7
        assert item.summary  # lead-text fallback is non-empty
