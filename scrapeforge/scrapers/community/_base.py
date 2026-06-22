"""CommunityScraper — abstract base for Bucket 2 (community / foreign sites).

SPEC.md §3.9 — per-bucket intermediate base.

Strategy
--------
- API-first (``curl_cffi``) for JSON endpoints (Reddit, Substack static).
- Browser escalation (``patchright`` / ``nodriver``) for JS-rendered content
  or Imperva — deferred to Phase 2.
- Proxy rotation per subreddit / newsletter (ProxyRotator handles this at the
  engine layer; the scraper just passes ``self.proxy`` through).

Invariants (SPEC.md §3.9)
--------------------------
- ``BUCKET = 'community'``
- ``DEFAULT_DRIVER = 'curl_cffi'``
- ``max_concurrency`` defaults to 5 (higher is safe for API endpoints).
- ``scrape()`` stays abstract — concrete platform scrapers implement it.
- ``_fetch_json`` is the shared helper for all API-endpoint subclasses.
"""

from __future__ import annotations

import json
import logging
from abc import abstractmethod

from scrapeforge.core.models import ScrapeResult
from scrapeforge.core.stealth_bridge import StealthBridge
from scrapeforge.scrapers.base import BaseScraper

log = logging.getLogger(__name__)


class CommunityScraper(BaseScraper):
    """Abstract base for all Bucket-2 community scrapers.

    Subclasses implement ``scrape()`` and optionally override ``scrape_<platform>``
    methods for paginated / bulk fetching.

    The shared helper ``_fetch_json`` abstracts the bridge lifecycle so concrete
    scrapers only deal with the JSON payload.
    """

    BUCKET: str = "community"
    DEFAULT_DRIVER: str = "curl_cffi"
    REQUIRES_AUTH: bool = False

    def __init__(
        self,
        bridge: StealthBridge | None = None,
        proxy: str | None = None,
        max_concurrency: int = 5,
    ) -> None:
        super().__init__(bridge=bridge, proxy=proxy, max_concurrency=max_concurrency)

    # ------------------------------------------------------------------
    # Abstract interface (inherited from BaseScraper)
    # ------------------------------------------------------------------

    @abstractmethod
    async def scrape(self, url: str) -> ScrapeResult:
        """Scrape a single URL.  Each community platform implements this."""
        ...

    # ------------------------------------------------------------------
    # Shared JSON-fetch helper
    # ------------------------------------------------------------------

    async def _fetch_json(self, url: str) -> dict | list:
        """Fetch *url* via the bridge and parse the response body as JSON.

        Bridge lifecycle
        ----------------
        - If ``self.bridge`` is set (injected, e.g. in tests), use it.
        - Otherwise create a fresh one-shot bridge per call so concurrent
          callers never share a closing backend (Invariant #7).

        Returns
        -------
        dict | list
            Parsed JSON.

        Raises
        ------
        json.JSONDecodeError
            If the response body is not valid JSON.
        scrapeforge.exceptions.DriverError
            Re-raised from the bridge if navigation fails.
        """
        bridge = self.bridge if self.bridge is not None else self._create_default_bridge(self.proxy)
        async with bridge as b:
            await b.navigate(url)
            html = await b.get_html()
            return json.loads(html)
