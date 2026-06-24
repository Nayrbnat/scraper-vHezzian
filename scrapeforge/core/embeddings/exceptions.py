"""Typed embedding errors — a sub-hierarchy under ScrapeForgeError (seam rule).

Defined here (not ``exceptions.py``) per the seam rule: subclass the base hierarchy
inside your feature. ``except EmbeddingError`` catches every embedding failure.
"""

from __future__ import annotations

from scrapeforge.exceptions import ScrapeForgeError


class EmbeddingError(ScrapeForgeError):
    """Any embedding provider/parse failure."""


class EmbeddingRateLimitError(EmbeddingError):
    """Provider rate-limited the request (HTTP 429) after retries were exhausted."""


class EmbeddingParseError(EmbeddingError):
    """The provider response could not be parsed into vectors."""
