"""build_user_digest preserves cosine order and attaches shared 1-10 relevance + bullets."""

from __future__ import annotations

from scrapeforge.core.models import Article
from scrapeforge.digest.user_source import ActiveUser


def _art(slug: str, relevance: int) -> Article:
    return Article(
        url=f"https://e.com/{slug}",
        title=f"Title {slug}",
        content="Body.",
        author=None,
        publish_date=None,
        metadata={
            "relevance": relevance,
            "summary": {"bullets": [f"b-{slug}", "b2"], "reason": "r"},
        },
    )


def test_build_user_digest_preserves_order_and_fields() -> None:
    from scrapeforge.digest.user_digest import build_user_digest

    user = ActiveUser(user_id="u1", email="a@e.com", name="a")
    # input already cosine-ordered by the query: first, second
    digest = build_user_digest(user, [_art("first", 9), _art("second", 6)])

    assert digest.subscriber_id == "u1"
    assert digest.subscriber_email == "a@e.com"
    assert len(digest.sections) == 1
    section = digest.sections[0]
    assert section.key == "top" and section.heading == "Top updates"
    assert [i.title for i in section.items] == ["Title first", "Title second"]  # order preserved
    assert section.items[0].relevance == 9
    assert section.items[0].bullets == ["b-first", "b2"]
    assert section.items[0].reason == "r"


def test_build_user_digest_empty_is_empty() -> None:
    from scrapeforge.digest.user_digest import build_user_digest

    digest = build_user_digest(ActiveUser("u1", "a@e.com", "a"), [])
    assert digest.is_empty
    assert digest.sections == []
