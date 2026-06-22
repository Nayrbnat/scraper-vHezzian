"""PublicScraper — Bucket 3: generic public news (SPEC.md §3.10).

Strategy: curl_cffi as default (80 % of targets).  Hybrid patchright escalation
(solve once, resume with curl_cffi) is deferred to Phase 2.

Invariants
----------
- ``DEFAULT_DRIVER`` is ``'curl_cffi'``.
- ``max_concurrency`` defaults to 5.
- Domain-agnostic: generic CSS selector fallback chains cover most WordPress /
  news sites.
- NOT decorated with ``@register_scraper`` — it is the catch-all the engine
  falls back to when ``get_scraper_for(domain)`` returns ``None`` (SPEC.md §3.17).
- ``ChallengeError`` is *not* caught here — it propagates to the engine so the
  engine can record the failure and (in a future phase) escalate.

Notes
-----
Phase-2 hybrid escalation hook::

    # TODO Phase 2: escalate to patchright then resume curl_cffi
    # 1. Create patchright bridge, solve challenge, export cookies.
    # 2. Create new curl_cffi bridge with those cookies.
    # 3. Re-navigate and extract article.
"""

from __future__ import annotations

import logging

from scrapeforge.core.models import ScrapeResult
from scrapeforge.scrapers.base import BaseScraper

log = logging.getLogger(__name__)


class PublicScraper(BaseScraper):
    """Generic scraper for public news outlets (Bucket 3 catch-all).

    Uses generic CSS selector chains that work on the majority of
    WordPress / news-themed sites.  Domain-specific scrapers registered
    via ``@register_scraper`` take priority; ``PublicScraper`` only runs
    when ``get_scraper_for(domain)`` returns ``None``.
    """

    BUCKET: str = "public"
    DEFAULT_DRIVER: str = "curl_cffi"
    REQUIRES_AUTH: bool = False

    def __init__(
        self,
        bridge=None,
        proxy: str | None = None,
        max_concurrency: int = 5,
    ) -> None:
        super().__init__(bridge=bridge, proxy=proxy, max_concurrency=max_concurrency)

    # ------------------------------------------------------------------
    # scrape
    # ------------------------------------------------------------------

    async def scrape(self, url: str) -> ScrapeResult:
        """Scrape a single public URL with curl_cffi.

        Flow
        ----
        1. Obtain a bridge: use the injected one (test override) OR create a
           FRESH default bridge per call.  A fresh bridge per call ensures that
           concurrent invocations from ``batch_scrape`` never share a single
           closing backend (Invariant #7).
        2. ``async with bridge as b`` launches the backend.
        3. Navigate to *url*.
        4. Retrieve HTML.
        5. Extract article via ``_extract_article`` (raises ``ChallengeError``
           on soft-block — engine handles escalation).
        6. Return ``ScrapeResult(status='success', ...)``.

        ``ChallengeError`` is intentionally *not* caught here (Phase-1 design).
        The engine catches it and records ``status='challenge'``.

        # TODO Phase 2: escalate to patchright then resume curl_cffi
        """
        bridge = self.bridge if self.bridge is not None else self._create_default_bridge(self.proxy)

        async with bridge as b:
            await b.navigate(url)
            html = await b.get_html()
            article = self._extract_article(html, url)
            return ScrapeResult(
                status="success",
                driver_used=b.driver,
                article=article,
                proxy_used=self.proxy,
            )

    # ------------------------------------------------------------------
    # Selectors — generic fallback chains
    # ------------------------------------------------------------------

    def _get_selectors(self) -> dict:
        """Generic CSS selector chains for WordPress / news-themed sites.

        Each value is a comma-separated fallback chain tried left-to-right.
        """
        return {
            "title": "h1.entry-title, h1.article-title, h1.post-title, h1",
            "content": "div.entry-content, article, div.post-content, div.content",
            "author": "span.author, a[rel=author], .byline",
            "publish_date": "time[datetime], span.date, .published",
        }
