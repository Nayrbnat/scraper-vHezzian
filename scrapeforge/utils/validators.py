"""Response / soft-block validators (SPEC.md §3.20, SRP).

Responsibility: decide whether an HTTP-200 response is a genuine article page
or an anti-bot decoy/honeypot.

Scope: **response/soft-block only**.
- JA4/TLS fingerprint validation lives on ``FingerprintManager.validate_ja4()``.
- DOM parsing lives in ``utils.parsers`` — no selectolax calls here.

A scraper calling ``response_is_valid(...) == False`` should raise
``ChallengeError`` so the normal escalation ladder triggers (Invariant #15).
"""

from __future__ import annotations

from scrapeforge.utils.parsers import main_text

# ---------------------------------------------------------------------------
# Known anti-bot / challenge-page text signatures (case-insensitive substring
# match against the raw HTML).
# ---------------------------------------------------------------------------
_BLOCK_SIGNATURES: tuple[str, ...] = (
    "just a moment",
    "request unsuccessful. incapsula incident",  # full Incapsula page title
    "attention required! | cloudflare",  # full Cloudflare page title
    "cf-browser-verification",
    "_cf_chl_opt",
    "imperva",
    "incapsula",
    "checking your browser",
)


def response_is_valid(
    html: str,
    selectors: dict[str, str | None],
    min_content_len: int = 500,
) -> bool:
    """Return ``True`` iff *html* appears to be a genuine content page.

    Returns ``False`` (treat as soft block) when **any** of:

    1. The ``selectors['content']`` key is missing, ``None``, or matches no DOM
       node — we cannot verify content, so we conservatively fail.
    2. The extracted content text length is below *min_content_len*.
    3. The raw HTML contains any known anti-bot challenge signature
       (case-insensitive substring check).

    Args:
        html:            Raw HTML string from the HTTP response.
        selectors:       CSS selector map; ``selectors.get('content')`` is used
                         to locate the main article body.
        min_content_len: Minimum character count for valid content (default 500).

    Returns:
        ``bool`` — ``True`` means the response looks genuine; ``False`` means
        treat it as a soft block / challenge page.
    """
    # --- Check 1: known block-page signatures (cheap string scan first) ---
    html_lower = html.lower()
    for sig in _BLOCK_SIGNATURES:
        if sig in html_lower:
            return False

    # --- Check 2: content selector present and matches a DOM node ---
    content_css = selectors.get("content")
    if content_css is None:
        return False

    content_text = main_text(html, content_css)
    if not content_text:
        # main_text returns "" when the selector matches nothing
        return False

    # --- Check 3: content length gate ---
    return len(content_text) >= min_content_len
