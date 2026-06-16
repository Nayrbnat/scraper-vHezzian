"""Typed exception hierarchy for ScrapeForge.

Every error raised by the library is a subclass of ``ScrapeForgeError``, so a
single ``except ScrapeForgeError`` handler at the call-site catches everything.
The hierarchy is intentionally flat — one level of subclasses only.

Usage example::

    try:
        result = await engine.scrape(url)
    except ChallengeError:
        # escalate to browser driver
        ...
    except ScrapeForgeError as exc:
        # catch-all for any other library error
        log.error("scrape failed: %s", exc)
"""

from __future__ import annotations


class ScrapeForgeError(Exception):
    """Base class for all ScrapeForge errors."""


class DriverError(ScrapeForgeError):
    """Driver-level failure: navigation, I/O, launch, or bridge-factory miss."""


class AuthError(ScrapeForgeError):
    """Authentication or StateStore failure."""


class ProxyError(ScrapeForgeError):
    """Proxy rotation or health-check failure."""


class ChallengeError(ScrapeForgeError):
    """Anti-bot challenge or soft-block detected (Cloudflare, Imperva, 200-decoy)."""


class RateLimitError(ScrapeForgeError):
    """Rate limit exceeded or HTTP 429 received."""


class FingerprintError(ScrapeForgeError):
    """Fingerprint or browser-profile configuration error."""
