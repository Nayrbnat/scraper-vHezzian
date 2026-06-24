"""The DigestItem gains bullets/relevance/reason; DigestSection gains the 'top' key."""

from __future__ import annotations


def test_digest_item_new_fields_default_empty() -> None:
    from scrapeforge.digest.models import DigestItem

    item = DigestItem(title="T", url="https://e.com/a", source="e.com", summary="s")
    assert item.bullets == []
    assert item.relevance is None
    assert item.reason is None


def test_digest_item_accepts_bullets_relevance_reason() -> None:
    from scrapeforge.digest.models import DigestItem

    item = DigestItem(
        title="T",
        url="https://e.com/a",
        source="e.com",
        summary="s",
        bullets=["b1", "b2"],
        relevance=9,
        reason="why",
    )
    assert item.bullets == ["b1", "b2"] and item.relevance == 9 and item.reason == "why"


def test_digest_section_top_key_allowed() -> None:
    from scrapeforge.digest.models import DigestSection

    section = DigestSection(key="top", heading="Top updates", items=[])
    assert section.key == "top"
