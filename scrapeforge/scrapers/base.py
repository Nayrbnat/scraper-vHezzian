"""Abstract base for all scrapers (SPEC.md §3.7).

Every scraper bucket (premium, community, public) inherits from ``BaseScraper``.
The base owns:
- Concurrency semaphore (``max_concurrency`` bound).
- ``batch_scrape`` with sink integration and seen-URL skipping.
- ``_extract_article`` thin coordinator (validate → parse → assemble).
- ``_create_default_bridge`` factory (no I/O — construction only).
- ``health_check`` skeleton.

Parsing lives in ``utils.parsers``; soft-block detection in ``utils.validators``;
the base only coordinates and assembles.  (SRP / SLAP — SPEC.md §3.7 note.)
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.stealth_bridge import StealthBridge
from scrapeforge.exceptions import ChallengeError
from scrapeforge.utils import parsers
from scrapeforge.utils.validators import response_is_valid

if TYPE_CHECKING:
    from scrapeforge.core.storage.base import ArticleSink

log = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base for all ScrapeForge scrapers.

    Invariants
    ----------
    - A single ``scrape()`` call uses ONE ``StealthBridge`` bound to ONE proxy
      (session affinity, Invariant #7).
    - ``batch_scrape`` bounds in-flight requests to *max_concurrency* via a
      semaphore; concurrent URLs do NOT share one session.
    - All I/O methods are ``async``; no blocking calls on the event loop.
    - Subclasses implement ``scrape()``; optionally override ``_get_selectors()``.
    """

    BUCKET: str = ""
    DOMAINS: list[str] = []
    DEFAULT_DRIVER: str = "curl_cffi"

    def __init__(
        self,
        bridge: StealthBridge | None = None,
        proxy: str | None = None,
        max_concurrency: int = 5,
    ) -> None:
        self.proxy = proxy
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # Store the injected bridge as-is (may be None).
        # Subclasses that need a bridge create a FRESH one per scrape() call via
        # _create_default_bridge — that ensures concurrent URLs never share a
        # single closing backend (Invariant #7).
        self.bridge: StealthBridge | None = bridge

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def scrape(self, url: str) -> ScrapeResult:
        """Scrape a single URL and return a ``ScrapeResult``.

        Must: navigate, extract ``Article``, return ``ScrapeResult``.
        May: raise ``ChallengeError`` so the engine escalates.
        """
        ...

    # ------------------------------------------------------------------
    # Batch scrape with semaphore + sink
    # ------------------------------------------------------------------

    async def batch_scrape(
        self,
        urls: list[str],
        sink: ArticleSink | None = None,
    ) -> list[ScrapeResult]:
        """Scrape all *urls* concurrently, bounded by ``self._semaphore``.

        - If *sink* is provided, URLs for which ``sink.seen(url)`` is ``True``
          are skipped.
        - Each successful result is persisted via ``await sink.write(result)``.
        - Exceptions from individual ``scrape()`` calls are captured into error
          ``ScrapeResult`` objects and never propagated (crash-safe gather).

        Returns
        -------
        list[ScrapeResult]
            One result per URL that was attempted (seen URLs are omitted from
            the returned list to match the promise that they were skipped).
        """
        tasks = []
        filtered_urls = []

        for url in urls:
            if sink is not None and sink.seen(url):
                log.debug("batch_scrape: skipping seen URL %s", url)
                continue
            filtered_urls.append(url)
            tasks.append(self._guarded_scrape(url, sink=sink))

        return list(await asyncio.gather(*tasks))

    async def _guarded_scrape(
        self,
        url: str,
        sink: ArticleSink | None = None,
    ) -> ScrapeResult:
        """Run ``scrape(url)`` under the semaphore; capture exceptions as error results."""
        async with self._semaphore:
            try:
                result = await self.scrape(url)
                if sink is not None and result.status == "success":
                    await sink.write(result)
                return result
            except Exception as exc:  # noqa: BLE001
                log.warning("batch_scrape error for %s: %s", url, exc)
                return ScrapeResult(
                    status="error",
                    driver_used=self.DEFAULT_DRIVER,
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Navigate the first domain root and return True iff it succeeds."""
        if not self.DOMAINS:
            return False
        url = f"https://{self.DOMAINS[0]}/"
        try:
            bridge = (
                self.bridge if self.bridge is not None else self._create_default_bridge(self.proxy)
            )
            async with bridge as b:
                result = await b.navigate(url)
                return result.status == "success"
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Bridge factory (no I/O)
    # ------------------------------------------------------------------

    def _create_default_bridge(self, proxy: str | None) -> StealthBridge:
        """Return a new ``StealthBridge`` with ``DEFAULT_DRIVER``.

        Construction only — no I/O.  The caller must use ``async with`` to
        launch the backend.
        """
        return StealthBridge(self.DEFAULT_DRIVER, proxy=proxy)

    # ------------------------------------------------------------------
    # Article extraction coordinator (SRP thin coordinator)
    # ------------------------------------------------------------------

    def _extract_article(self, html: str, url: str) -> Article:
        """Coordinate validation → parsing → assembly into an ``Article``.

        Does NOT own parsing logic (lives in ``parsers``) or soft-block
        detection (lives in ``validators``).  The boundary is:
        - ``validators.response_is_valid`` — is the response genuine?
        - ``parsers.extract``              — what are the field values?
        - Here                             — assemble the ``Article``.

        Raises
        ------
        ChallengeError
            When ``response_is_valid`` returns ``False`` (soft-block / decoy).
        """
        selectors = self._get_selectors()

        if not response_is_valid(html, selectors):
            raise ChallengeError(f"Soft block detected at {url} — response failed validation.")

        fields = parsers.extract(html, selectors)

        domain = urllib.parse.urlsplit(url).hostname or ""

        return Article(
            url=url,
            title=fields.get("title") or "",
            content=fields.get("content") or "",
            author=fields.get("author"),
            publish_date=None,
            metadata={
                "source_domain": domain,
                "bucket": self.BUCKET,
            },
        )

    # ------------------------------------------------------------------
    # Selector hook (subclasses override)
    # ------------------------------------------------------------------

    def _get_selectors(self) -> dict:
        """Return CSS selectors for field extraction.

        Keys used: ``title``, ``content``, ``author``, ``publish_date``.
        Default is empty (subclasses provide domain-specific chains).
        """
        return {}
