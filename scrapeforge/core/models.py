"""Core data structures for ScrapeForge (SPEC.md §2).

All dataclasses here are the single source of truth for the objects exchanged
between drivers, scrapers, the engine, and storage backends.

Frozen dataclasses (``Article``, ``ScrapeResult``, ``BrowserProfile``) are
immutable after construction, which prevents accidental in-place mutation across
async boundaries.  ``ProxySession`` and ``StorageState`` are mutable because
their health/validity fields are updated in place by ``ProxyRotator`` and
``AuthManager`` respectively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Article:
    """A scraped article record.  Immutable after construction.

    Metadata keys used elsewhere (not enforced here):
    ``source_domain``, ``bucket``, ``driver_used``, ``proxy_used``,
    ``challenge_solved`` (bool), ``fetch_duration_ms`` (int).
    """

    url: str
    title: str
    content: str  # cleaned text or markdown
    author: str | None = None
    publish_date: datetime | None = None
    raw_html: str | None = None  # optional, for debugging
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScrapeResult:
    """Outcome of a single scrape attempt.  Immutable after construction.

    ``status`` is one of: ``'success'``, ``'challenge'``, ``'rate_limited'``,
    ``'error'``, ``'proxy_failed'``.

    Non-default fields (``status``, ``driver_used``) are declared first so the
    dataclass does not raise ``TypeError`` at import time (SPEC.md §2.2 note).
    """

    # --- required (no defaults) ---
    status: str
    driver_used: str
    # --- optional (all have defaults) ---
    article: Article | None = None
    error: str | None = None
    proxy_used: str | None = None
    challenge_solved: bool = False
    retry_count: int = 0
    fetch_duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """Single source of truth for a session's fingerprint.

    All drivers in one handoff chain share the same ``BrowserProfile`` so the
    JA4 / UA / platform stay coherent (SPEC.md Invariant #11).

    ``http2_settings`` has no default and precedes the defaulted fields, which
    satisfies the dataclass field-ordering constraint.
    """

    name: str  # e.g. 'chrome_win', 'chrome_mac', 'chrome_linux'
    user_agent: str
    tls_fingerprint: str  # JA4 hash or raw description
    http2_settings: dict  # SETTINGS_HEADER_TABLE_SIZE, INITIAL_WINDOW_SIZE, etc.
    platform: str  # 'Win32' | 'MacIntel' | 'Linux x86_64'
    chrome_major_version: int  # resolved from installed Chrome; binds curl_cffi impersonate target
    accept_language: str = "en-US,en;q=0.9"
    viewport: tuple[int, int] = (1920, 1080)


@dataclass(slots=True)
class ProxySession:
    """Tracks state for a single proxy URL across its lifecycle.

    Intentionally mutable — ``ProxyRotator`` updates ``health_status`` and
    ``failure_count`` in place without replacing the instance.
    """

    url: str  # protocol://user:pass@host:port
    health_status: str = "unknown"  # 'healthy' | 'unhealthy' | 'burned' | 'unknown'
    last_used: datetime | None = None
    failure_count: int = 0
    assigned_scraper: str | None = None  # scraper class name
    country_code: str | None = None


@dataclass(slots=True)
class StorageState:
    """Persisted browser session: cookies + Web Storage for one domain.

    Intentionally mutable — ``StateStore`` may mark ``is_valid = False`` when a
    session expires without needing a full replacement.

    ``created_at`` is always timezone-aware (UTC).  ``datetime.utcnow()`` is
    deprecated in Python 3.12+ and is explicitly forbidden here.
    """

    domain: str
    cookies: list[dict] = field(default_factory=list)
    local_storage: dict = field(default_factory=dict)
    session_storage: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None  # TTL; default +7 days (set by caller)
    is_valid: bool = True
