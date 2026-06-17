"""ScrapeEngine — main orchestrator (SPEC.md §3.17).

Responsibilities (one level of abstraction — SLAP):
  route → breaker → rate-limit → proxy → scrape → record → sink

The engine owns NO resilience state itself:
- Proactive politeness is delegated to ``RateLimiter``.
- Reactive circuit-breaking is delegated to ``CircuitBreaker``.
- Routing is via ``core.registry.get_scraper_for(domain)`` — NOT a central dict.

Dependency-injection design
---------------------------
All collaborators are constructor parameters with sensible defaults so tests can
inject fakes without touching the environment or the network.  ``discover=True``
triggers ``discover_scrapers()`` on init (set to ``False`` in tests that
register their own dummy scrapers to avoid importing real scraper modules).

NOTE: ``AuthManager`` is Phase 4 / out of scope — the constructor does NOT
instantiate it.  A comment marks the reserved slot.

NOTE: ``FingerprintManager`` is accepted as an injected parameter but not yet
used by the engine itself.  It is kept here so the constructor signature is
stable for future phases.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import TYPE_CHECKING

from scrapeforge.core.circuit_breaker import CircuitBreaker
from scrapeforge.core.fingerprint_manager import FingerprintManager
from scrapeforge.core.models import ScrapeResult
from scrapeforge.core.proxy_rotator import ProxyRotator
from scrapeforge.core.rate_limiter import RateLimiter
from scrapeforge.core.registry import discover_scrapers, get_scraper_for
from scrapeforge.exceptions import (
    ChallengeError,
    DriverError,
    ProxyError,
    RateLimitError,
    ScrapeForgeError,
)

if TYPE_CHECKING:
    from scrapeforge.core.storage.base import ArticleSink

log = logging.getLogger(__name__)


class ScrapeEngine:
    """Main orchestrator — routes URLs, applies resilience policy, persists results.

    Parameters
    ----------
    sink:
        Optional ``ArticleSink`` for persisting successful results.
    proxy_rotator:
        ``ProxyRotator`` instance.  Defaults to a fresh ``ProxyRotator()`` when
        ``None``; inject a fake in tests.
    rate_limiter:
        ``RateLimiter`` instance.  Defaults to ``RateLimiter()`` when ``None``.
    circuit_breaker:
        ``CircuitBreaker`` instance.  Defaults to ``CircuitBreaker()`` when ``None``.
    fingerprint_manager:
        ``FingerprintManager`` instance.  Reserved for Phase 2+ use.
    discover:
        When ``True`` (default), calls ``discover_scrapers()`` so every
        ``@register_scraper`` decorator runs.  Set to ``False`` in tests that
        manually populate the registry with dummies.
    """

    def __init__(
        self,
        sink: ArticleSink | None = None,
        *,
        proxy_rotator: ProxyRotator | None = None,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        fingerprint_manager: FingerprintManager | None = None,
        discover: bool = True,
    ) -> None:
        if discover:
            discover_scrapers()

        self.sink = sink
        self.proxy_rotator: ProxyRotator = proxy_rotator or ProxyRotator()
        self.rate_limiter: RateLimiter = rate_limiter or RateLimiter()
        self.circuit_breaker: CircuitBreaker = circuit_breaker or CircuitBreaker()
        self.fingerprint_manager: FingerprintManager = fingerprint_manager or FingerprintManager()

        # NOTE: AuthManager is Phase 4 — omitted intentionally.
        # self.auth_manager = AuthManager()

    # ------------------------------------------------------------------
    # Single-URL scrape
    # ------------------------------------------------------------------

    async def scrape(self, url: str) -> ScrapeResult:
        """Scrape *url* through the full resilience pipeline.

        Flow (one level of abstraction)
        --------------------------------
        1. Parse domain.
        2. Circuit-breaker gate (short-circuit if domain is paused).
        3. Rate-limiter acquire (proactive politeness).
        4. Route to registered scraper or fall back to ``PublicScraper``.
        5. Assign a healthy proxy.
        6. Instantiate and dispatch the scraper.
        7. Record outcome in circuit breaker.
        8. Persist via sink on success.
        9. Return result.
        """
        # 1. Parse domain
        domain = urllib.parse.urlsplit(url).hostname or ""

        # 2. Circuit-breaker gate
        if not self.circuit_breaker.allow(domain):
            log.debug("Circuit breaker open for %s — skipping %s", domain, url)
            return ScrapeResult(
                status="error",
                driver_used="none",
                error=f"circuit breaker open for {domain}",
            )

        # 3. Rate-limiter acquire
        await self.rate_limiter.acquire(domain)

        # 4. Route: registered scraper or PublicScraper catch-all
        # Import here (inside the method) to avoid a circular import at module
        # level; engine.py imports from scrapers, scrapers import from core.
        from scrapeforge.scrapers.public.public import PublicScraper  # noqa: PLC0415

        scraper_cls = get_scraper_for(domain) or PublicScraper

        # 5. Assign proxy
        proxy_session = await self.proxy_rotator.get_healthy_proxy()
        proxy = proxy_session.url if proxy_session is not None else None

        # 6. Instantiate scraper
        scraper = scraper_cls(proxy=proxy)

        # 7. Dispatch and map exceptions to result statuses
        result: ScrapeResult
        try:
            result = await scraper.scrape(url)
        except ChallengeError as exc:
            log.info("Challenge detected for %s: %s", url, exc)
            result = ScrapeResult(
                status="challenge",
                driver_used=getattr(scraper_cls, "DEFAULT_DRIVER", "curl_cffi"),
                error=str(exc),
            )
        except RateLimitError as exc:
            log.info("Rate limit for %s: %s", url, exc)
            result = ScrapeResult(
                status="rate_limited",
                driver_used=getattr(scraper_cls, "DEFAULT_DRIVER", "curl_cffi"),
                error=str(exc),
            )
        except ProxyError as exc:
            log.warning("Proxy failure for %s: %s", url, exc)
            result = ScrapeResult(
                status="proxy_failed",
                driver_used=getattr(scraper_cls, "DEFAULT_DRIVER", "curl_cffi"),
                error=str(exc),
            )
        except (DriverError, ScrapeForgeError) as exc:
            log.warning("Scrape error for %s: %s", url, exc)
            result = ScrapeResult(
                status="error",
                driver_used=getattr(scraper_cls, "DEFAULT_DRIVER", "curl_cffi"),
                error=str(exc),
            )

        # 8. Record outcome in circuit breaker
        self.circuit_breaker.record(domain, result.status == "success")

        # 9. Persist on success
        if result.status == "success" and self.sink is not None:
            await self.sink.write(result)

        return result

    # ------------------------------------------------------------------
    # Batch scrape
    # ------------------------------------------------------------------

    async def batch_scrape(self, urls: list[str]) -> list[ScrapeResult]:
        """Scrape all *urls* concurrently via ``asyncio.gather``.

        Each URL goes through the full ``scrape()`` pipeline independently
        (rate-limit, circuit-breaker, proxy assignment, etc.).

        Per-domain grouping / further optimisation is a later phase.
        """
        return list(await asyncio.gather(*[self.scrape(u) for u in urls]))
