"""DOM extraction utilities — the SOLE owner of selectolax-based parsing (SPEC.md §3.20, SRP).

Responsibility: HTML string → field dict.  Nothing else.

- No validation logic (that is ``utils/validators.py``).
- No Article assembly (that is ``BaseScraper._extract_article``).
- No JA4/TLS concerns (that is ``FingerprintManager``).

Comma-separated selector strings are supported to allow graceful fallback chains,
e.g. ``'h1.entry-title, h1.article-title, h1'`` tries each selector in order and
returns the text of the first match.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser


def extract(
    html: str,
    selectors: dict[str, str | None],
) -> dict[str, str | None]:
    """Extract text fields from *html* using CSS selectors.

    For each ``(field, css)`` pair in *selectors*:

    - If *css* is ``None`` → the field maps to ``None`` in the result.
    - Otherwise *css* may be a comma-separated list of selectors.  Each is
      tried in order; the text of the *first* match is returned.
    - If no selector in the chain matches → the field maps to ``None``.

    Whitespace is stripped from all extracted text.

    Args:
        html:      Raw HTML string to parse.
        selectors: Mapping of field name → CSS selector (or ``None``).

    Returns:
        Dict with the same keys as *selectors*; values are stripped text or
        ``None``.
    """
    tree = HTMLParser(html)
    result: dict[str, str | None] = {}

    for field, css in selectors.items():
        result[field] = _first_match(tree, css)

    return result


def main_text(html: str, content_selector: str | None) -> str:
    """Return the text content of the first matching node.

    Used by ``validators.response_is_valid`` to measure content length.

    Args:
        html:             Raw HTML string.
        content_selector: CSS selector (may be comma-separated) or ``None``.

    Returns:
        Stripped text of the matched node, or ``""`` if nothing matched.
    """
    if content_selector is None:
        return ""
    tree = HTMLParser(html)
    text = _first_match(tree, content_selector)
    return text if text is not None else ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_match(tree: HTMLParser, css: str | None) -> str | None:
    """Return stripped text of the first DOM node matched by *css*.

    Supports comma-separated fallback selector chains.  Returns ``None`` when
    *css* is ``None`` or no selector in the chain matches.
    """
    if css is None:
        return None

    # Split on commas, strip surrounding whitespace from each part
    candidates = [s.strip() for s in css.split(",") if s.strip()]

    for selector in candidates:
        node = tree.css_first(selector)
        if node is not None:
            text = node.text(deep=True)
            return text.strip() if text else None

    return None
