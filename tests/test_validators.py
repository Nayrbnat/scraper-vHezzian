"""Tests for scrapeforge.utils.validators — response/soft-block detection.

TDD: written before implementation.

Coverage targets:
- A real-looking article (long content + matching selector) -> True.
- Short content -> False.
- Missing content node (selector yields nothing) -> False.
- Each known block-page signature -> False.
- import from parsers is used; no DOM logic duplicated here.
- Regression: benign prose containing "attention required" is NOT flagged.
"""

from __future__ import annotations

import pytest

from scrapeforge.utils.validators import response_is_valid

# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

# A real-looking article with sufficient content
_LONG_CONTENT = "This is a well-formed article paragraph. " * 20  # ~820 chars

VALID_ARTICLE_HTML = f"""
<!DOCTYPE html>
<html>
<head><title>Valid Article</title></head>
<body>
  <h1>Article Title</h1>
  <div class="entry-content">
    <p>{_LONG_CONTENT}</p>
  </div>
</body>
</html>
"""

SHORT_CONTENT_HTML = """
<html><body>
  <div class="entry-content"><p>Too short.</p></div>
</body></html>
"""

NO_CONTENT_NODE_HTML = """
<html><body>
  <h1>Title but no content div</h1>
  <p>Some paragraph without the expected selector.</p>
</body></html>
"""

# Standard selectors used in tests
SELECTORS = {
    "title": "h1",
    "content": "div.entry-content",
    "author": "span.author",
}

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestResponseIsValidTrue:
    def test_valid_article_returns_true(self) -> None:
        """A full-length article with a matching content selector returns True."""
        result = response_is_valid(VALID_ARTICLE_HTML, SELECTORS)
        assert result is True

    def test_custom_min_length_lower_threshold(self) -> None:
        """min_content_len=0 makes even short content pass (assuming no block signatures)."""
        result = response_is_valid(SHORT_CONTENT_HTML, SELECTORS, min_content_len=0)
        assert result is True


# ---------------------------------------------------------------------------
# Content length failures
# ---------------------------------------------------------------------------


class TestResponseIsValidShortContent:
    def test_short_content_returns_false(self) -> None:
        """Content shorter than min_content_len -> False."""
        result = response_is_valid(SHORT_CONTENT_HTML, SELECTORS)
        assert result is False

    def test_exactly_at_threshold_is_false(self) -> None:
        """Content length == min_content_len - 1 -> False."""
        exactly_499 = "x" * 499
        html = f'<html><body><div class="entry-content">{exactly_499}</div></body></html>'
        result = response_is_valid(html, SELECTORS, min_content_len=500)
        assert result is False

    def test_exactly_at_min_is_true(self) -> None:
        """Content length == min_content_len -> True (at or above threshold)."""
        exactly_500 = "x" * 500
        html = f'<html><body><div class="entry-content">{exactly_500}</div></body></html>'
        result = response_is_valid(html, SELECTORS, min_content_len=500)
        assert result is True


# ---------------------------------------------------------------------------
# Missing content node
# ---------------------------------------------------------------------------


class TestResponseIsValidMissingNode:
    def test_no_content_node_returns_false(self) -> None:
        """When the content selector matches nothing, return False."""
        result = response_is_valid(NO_CONTENT_NODE_HTML, SELECTORS)
        assert result is False

    def test_none_content_selector_returns_false(self) -> None:
        """When content key maps to None, return False (can't verify content length)."""
        selectors_no_content = {"title": "h1", "content": None}
        result = response_is_valid(VALID_ARTICLE_HTML, selectors_no_content)
        assert result is False

    def test_missing_content_key_returns_false(self) -> None:
        """Selectors dict without 'content' key -> no content -> False."""
        selectors_no_key = {"title": "h1"}
        result = response_is_valid(VALID_ARTICLE_HTML, selectors_no_key)
        assert result is False


# ---------------------------------------------------------------------------
# Known block-page signatures
#
# NOTE: "attention required" and "request unsuccessful" are bare substrings
# that appear in legitimate prose.  The signatures now require the fuller
# anti-bot page titles to avoid false positives:
#   "attention required! | cloudflare"
#   "request unsuccessful. incapsula incident"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html_fragment",
    [
        # Cloudflare "Just a moment" challenge
        "just a moment",
        "Just A Moment",  # case-insensitive
        # Incapsula incident page — must use the FULL title string
        "request unsuccessful. incapsula incident",
        "REQUEST UNSUCCESSFUL. INCAPSULA INCIDENT",
        # Cloudflare "Attention Required" — must use the FULL title string
        "attention required! | cloudflare",
        "Attention Required! | Cloudflare",
        # Cloudflare browser-verification challenge attribute
        "cf-browser-verification",
        # Cloudflare challenge JS variable
        "_cf_chl_opt",
        # Generic Imperva / Incapsula tokens
        "imperva",
        "Imperva",
        "incapsula",
        "INCAPSULA",
        # Generic browser-check message
        "checking your browser",
        "Checking Your Browser",
    ],
)
def test_block_page_signature_returns_false(html_fragment: str) -> None:
    """Any known anti-bot signature in the HTML -> False regardless of content."""
    html = f"""
    <html><body>
      <div class="entry-content">
        <p>{_LONG_CONTENT}</p>
        <p>{html_fragment}</p>
      </div>
    </body></html>
    """
    result = response_is_valid(html, SELECTORS)
    assert result is False, f"Expected False for block signature: {html_fragment!r}"


class TestBlockSignatureEdgeCases:
    def test_cloudflare_script_in_head_detected(self) -> None:
        """Block signature in <head> is still caught."""
        html = f"""
        <html>
        <head><script>var _cf_chl_opt = {{}};</script></head>
        <body>
          <div class="entry-content"><p>{_LONG_CONTENT}</p></div>
        </body>
        </html>
        """
        result = response_is_valid(html, SELECTORS)
        assert result is False

    def test_imperva_in_script_tag_detected(self) -> None:
        """Imperva signature inside a script tag is caught."""
        html = f"""
        <html>
        <head><script>/* imperva protection */</script></head>
        <body>
          <div class="entry-content"><p>{_LONG_CONTENT}</p></div>
        </body>
        </html>
        """
        result = response_is_valid(html, SELECTORS)
        assert result is False


# ---------------------------------------------------------------------------
# Regression: bare substrings must NOT cause false positives
# ---------------------------------------------------------------------------


class TestFalsePositiveRegression:
    def test_bare_attention_required_in_prose_not_flagged(self) -> None:
        """An article body that happens to contain "attention required" in normal
        prose must NOT be treated as a block page.

        The real Cloudflare page uses the full title
        "Attention Required! | Cloudflare", so requiring that fuller string
        eliminates the false positive.
        """
        prose = (
            "The report stressed that attention required from all stakeholders "
            "is substantial.  " * 25
        )  # ~1300 chars — well above min_content_len=500
        html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Policy Analysis Report</title></head>
        <body>
          <h1>Policy Analysis</h1>
          <div class="entry-content"><p>{prose}</p></div>
        </body>
        </html>
        """
        result = response_is_valid(html, SELECTORS)
        assert result is True, (
            'Bare "attention required" in article prose caused a false block detection'
        )

    def test_bare_request_unsuccessful_in_prose_not_flagged(self) -> None:
        """An article body that mentions "request unsuccessful" in normal prose
        must NOT be treated as a block page.

        The real Incapsula page uses the fuller string
        "request unsuccessful. incapsula incident".
        """
        prose = (
            "The diplomatic request unsuccessful in its aims was widely reported. " * 15
        )  # ~1050 chars
        html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Diplomatic News</title></head>
        <body>
          <h1>Diplomatic Failure</h1>
          <div class="entry-content"><p>{prose}</p></div>
        </body>
        </html>
        """
        result = response_is_valid(html, SELECTORS)
        assert result is True, (
            'Bare "request unsuccessful" in article prose caused a false block detection'
        )
