"""Tests for scrapeforge.utils.parsers — DOM extraction.

TDD: written before implementation.

Coverage targets:
- extract() returns correct text for each matched field.
- Comma-separated fallback selectors: picks the first present one.
- Missing selector (no match) -> field is None.
- None css -> field is None.
- main_text() returns concatenated content node text.
- Whitespace is stripped from extracted text.
"""

from __future__ import annotations

from scrapeforge.utils.parsers import extract, main_text

# ---------------------------------------------------------------------------
# HTML fixture
# ---------------------------------------------------------------------------

FULL_ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Article</title></head>
<body>
  <h1 class="entry-title">Big News Today</h1>
  <span class="author">Jane Doe</span>
  <time datetime="2024-03-15T10:00:00Z">March 15, 2024</time>
  <div class="entry-content">
    <p>This is the first paragraph of a long article about important events.</p>
    <p>More content here with lots of words to satisfy the minimum length check.</p>
  </div>
  <div class="sidebar">Not content</div>
</body>
</html>
"""

MINIMAL_HTML = """
<html><body>
  <h1>Simple Title</h1>
  <p class="body-text">Some body text here.</p>
</body></html>
"""

# ---------------------------------------------------------------------------
# extract() — happy path
# ---------------------------------------------------------------------------


class TestExtract:
    """Tests for the main extract() function."""

    def test_extracts_title(self) -> None:
        result = extract(FULL_ARTICLE_HTML, {"title": "h1.entry-title"})
        assert result["title"] == "Big News Today"

    def test_extracts_author(self) -> None:
        result = extract(FULL_ARTICLE_HTML, {"author": "span.author"})
        assert result["author"] == "Jane Doe"

    def test_extracts_publish_date(self) -> None:
        result = extract(FULL_ARTICLE_HTML, {"publish_date": "time[datetime]"})
        assert result["publish_date"] == "March 15, 2024"

    def test_extracts_content(self) -> None:
        result = extract(FULL_ARTICLE_HTML, {"content": "div.entry-content"})
        assert result["content"] is not None
        assert "first paragraph" in result["content"]

    def test_all_fields_together(self) -> None:
        selectors = {
            "title": "h1.entry-title",
            "author": "span.author",
            "publish_date": "time[datetime]",
            "content": "div.entry-content",
        }
        result = extract(FULL_ARTICLE_HTML, selectors)
        assert result["title"] == "Big News Today"
        assert result["author"] == "Jane Doe"
        assert result["publish_date"] == "March 15, 2024"
        assert result["content"] is not None

    def test_strips_whitespace(self) -> None:
        html = "<html><body><h1>  Padded Title  </h1></body></html>"
        result = extract(html, {"title": "h1"})
        assert result["title"] == "Padded Title"

    def test_returns_dict_with_same_keys(self) -> None:
        selectors = {"title": "h1", "author": "span.missing", "extra": None}
        result = extract(FULL_ARTICLE_HTML, selectors)
        assert set(result.keys()) == {"title", "author", "extra"}


# ---------------------------------------------------------------------------
# extract() — missing / None selectors
# ---------------------------------------------------------------------------


class TestExtractMissing:
    def test_none_css_returns_none(self) -> None:
        """None as the css value must produce None in the result."""
        result = extract(FULL_ARTICLE_HTML, {"title": None})
        assert result["title"] is None

    def test_unmatched_selector_returns_none(self) -> None:
        """A valid but non-matching selector returns None."""
        result = extract(FULL_ARTICLE_HTML, {"author": "span.nonexistent-class"})
        assert result["author"] is None

    def test_empty_selectors_dict_returns_empty(self) -> None:
        result = extract(FULL_ARTICLE_HTML, {})
        assert result == {}


# ---------------------------------------------------------------------------
# extract() — comma-separated fallback selectors
# ---------------------------------------------------------------------------


class TestExtractFallback:
    def test_first_matching_fallback_used(self) -> None:
        """When the first selector doesn't match but the second does, use the second."""
        result = extract(FULL_ARTICLE_HTML, {"title": "h1.article-title, h1.entry-title"})
        assert result["title"] == "Big News Today"

    def test_first_selector_wins_when_both_present(self) -> None:
        """When the first selector matches, use it (don't skip to the second)."""
        html = """
        <html><body>
          <h1 class="entry-title">Title A</h1>
          <h1 class="article-title">Title B</h1>
        </body></html>
        """
        result = extract(html, {"title": "h1.entry-title, h1.article-title"})
        assert result["title"] == "Title A"

    def test_last_fallback_in_chain_used(self) -> None:
        """Fall through all earlier misses to the final fallback."""
        result = extract(FULL_ARTICLE_HTML, {"title": "h1.nope, h2.nope, h1"})
        # The plain h1 should match
        assert result["title"] is not None

    def test_all_fallbacks_missing_returns_none(self) -> None:
        result = extract(
            FULL_ARTICLE_HTML,
            {"title": "h2.nope, h3.nope, span.nope"},
        )
        assert result["title"] is None

    def test_fallback_with_spaces_around_comma(self) -> None:
        """Spaces around commas in selector strings must be handled correctly."""
        result = extract(FULL_ARTICLE_HTML, {"title": "h1.nope , h1.entry-title"})
        assert result["title"] == "Big News Today"


# ---------------------------------------------------------------------------
# main_text()
# ---------------------------------------------------------------------------


class TestMainText:
    def test_returns_string(self) -> None:
        text = main_text(FULL_ARTICLE_HTML, "div.entry-content")
        assert isinstance(text, str)
        assert len(text) > 0

    def test_includes_paragraph_text(self) -> None:
        text = main_text(FULL_ARTICLE_HTML, "div.entry-content")
        assert "first paragraph" in text

    def test_none_selector_returns_empty(self) -> None:
        text = main_text(FULL_ARTICLE_HTML, None)
        assert text == ""

    def test_unmatched_selector_returns_empty(self) -> None:
        text = main_text(FULL_ARTICLE_HTML, "div.does-not-exist")
        assert text == ""

    def test_returns_string_for_simple_html(self) -> None:
        text = main_text(MINIMAL_HTML, "p.body-text")
        assert "body text" in text
