"""Renderer shows bullets+badge+reason for relevance items; falls back to the blurb otherwise."""

from __future__ import annotations

from datetime import UTC, datetime

from scrapeforge.digest.models import Digest, DigestItem, DigestSection


def _digest(item: DigestItem) -> Digest:
    return Digest(
        subscriber_id="dee",
        subscriber_name="Dee",
        subscriber_email="dee@example.com",
        period="2026-06-24",
        generated_at=datetime(2026, 6, 24, tzinfo=UTC),
        sections=[DigestSection(key="top", heading="Top updates", items=[item])],
    )


def test_html_renders_badge_bullets_reason() -> None:
    from scrapeforge.digest.render import render_html

    item = DigestItem(
        title="TSMC roadmap",
        url="https://e.com/a",
        source="e.com",
        summary="blurb",
        bullets=["bullet one", "bullet two", "bullet three"],
        relevance=9,
        reason="your niche; fresh",
    )
    html = render_html(_digest(item))
    assert "9/10" in html
    assert "bullet one" in html and "bullet two" in html and "bullet three" in html
    assert "your niche; fresh" in html
    assert "blurb" not in html  # lead-text blurb replaced by bullets


def test_text_renders_badge_bullets_reason() -> None:
    from scrapeforge.digest.render import render_text

    item = DigestItem(
        title="TSMC roadmap",
        url="https://e.com/a",
        source="e.com",
        summary="blurb",
        bullets=["bullet one", "bullet two"],
        relevance=8,
        reason="why",
    )
    text = render_text(_digest(item))
    assert "8/10" in text and "bullet one" in text and "why" in text


def test_keyword_item_without_bullets_still_renders_blurb() -> None:
    from scrapeforge.digest.render import render_html

    item = DigestItem(
        title="Old style",
        url="https://e.com/b",
        source="e.com",
        summary="the lead-text blurb",
        matched_on=["Stripe"],
    )
    html = render_html(_digest(item))
    assert "the lead-text blurb" in html  # legacy path unchanged
    assert "Stripe" in html  # matched_on chip
