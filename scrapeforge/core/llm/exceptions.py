"""Typed LLM errors — the summarizer's own sub-hierarchy under ScrapeForgeError.

Defined in this module (not ``exceptions.py``) per the seam rule: subclass the base
hierarchy inside your feature. ``except LLMError`` catches every LLM failure.
"""

from __future__ import annotations

from scrapeforge.exceptions import ScrapeForgeError


class LLMError(ScrapeForgeError):
    """Any LLM provider/parse failure."""


class LLMRateLimitError(LLMError):
    """Provider rate-limited the request (HTTP 429) after retries were exhausted."""


class LLMParseError(LLMError):
    """The provider response could not be parsed into a valid SummaryResult."""
