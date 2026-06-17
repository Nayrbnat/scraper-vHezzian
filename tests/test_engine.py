"""Tests for ScrapeEngine (SPEC.md §3.17).

TDD first — tests drive scrapeforge/core/engine.py.

All collaborators are injected as fakes; discover=False prevents real imports.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # noqa: F401 — used by pytest.mark.asyncio

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.exceptions import (
    ChallengeError,
    DriverError,
    ProxyError,
    RateLimitError,
    ScrapeForgeError,
)

# ---------------------------------------------------------------------------
# Helpers — fake collaborators
# ---------------------------------------------------------------------------


def _fake_rate_limiter():
    rl = MagicMock()
    rl.acquire = AsyncMock()
    return rl


def _fake_circuit_breaker(*, allow: bool = True):
    cb = MagicMock()
    cb.allow = MagicMock(return_value=allow)
    cb.record = MagicMock()
    return cb


def _fake_proxy_rotator(proxy_url: str | None = None):
    pr = MagicMock()
    if proxy_url is None:
        pr.get_healthy_proxy = AsyncMock(return_value=None)
    else:
        from scrapeforge.core.models import ProxySession

        pr.get_healthy_proxy = AsyncMock(return_value=ProxySession(url=proxy_url))
    return pr


def _fake_fingerprint_manager():
    fm = MagicMock()
    return fm


def _make_engine(
    *,
    allow: bool = True,
    proxy_url: str | None = None,
    sink=None,
):
    """Construct a ScrapeEngine with all fake collaborators."""
    from scrapeforge.core.engine import ScrapeEngine

    return ScrapeEngine(
        sink=sink,
        proxy_rotator=_fake_proxy_rotator(proxy_url),
        rate_limiter=_fake_rate_limiter(),
        circuit_breaker=_fake_circuit_breaker(allow=allow),
        fingerprint_manager=_fake_fingerprint_manager(),
        discover=False,
    )


def _good_article(url: str = "https://example.com") -> Article:
    return Article(url=url, title="Test", content="content " * 100)


def _success_result(url: str = "https://example.com") -> ScrapeResult:
    return ScrapeResult(
        status="success",
        driver_used="curl_cffi",
        article=_good_article(url),
    )


# ---------------------------------------------------------------------------
# Routing: registered scraper wins over PublicScraper fallback
# ---------------------------------------------------------------------------


class TestRouting:
    @pytest.mark.asyncio
    async def test_registered_scraper_used(self):
        from scrapeforge.core.registry import _REGISTRY
        from scrapeforge.scrapers.base import BaseScraper

        class _DummyScraper(BaseScraper):
            BUCKET = "test"
            DOMAINS = ["dummy.test"]
            DEFAULT_DRIVER = "curl_cffi"

            async def scrape(self, url: str) -> ScrapeResult:  # type: ignore[override]
                return _success_result(url)

        _REGISTRY["dummy.test"] = _DummyScraper
        try:
            engine = _make_engine()
            result = await engine.scrape("https://dummy.test/page")
            assert result.status == "success"
        finally:
            _REGISTRY.pop("dummy.test", None)

    @pytest.mark.asyncio
    async def test_unregistered_falls_back_to_public(self):
        """An unregistered domain should use PublicScraper as the catch-all."""
        from scrapeforge.scrapers.public.public import PublicScraper

        used_scraper_class = []

        original_init = PublicScraper.__init__

        def _patched_init(self, *args, **kwargs):
            used_scraper_class.append(type(self))
            original_init(self, *args, **kwargs)

        engine = _make_engine()
        with (
            patch.object(PublicScraper, "__init__", _patched_init),
            patch.object(
                PublicScraper,
                "scrape",
                new_callable=lambda: lambda self, *a, **kw: _async_return(_success_result()),
            ),
        ):
            await engine.scrape("https://unknown-domain.xyz/page")

        assert PublicScraper in used_scraper_class

    @pytest.mark.asyncio
    async def test_unregistered_domain_returns_result(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(
            PublicScraper,
            "scrape",
            return_value=_success_result("https://no-scraper.example/page"),
        ):
            result = await engine.scrape("https://no-scraper.example/page")
        assert isinstance(result, ScrapeResult)


# ---------------------------------------------------------------------------
# Circuit breaker gating
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_open_breaker_short_circuits(self):
        engine = _make_engine(allow=False)
        result = await engine.scrape("https://blocked.com/page")
        assert result.status == "error"
        assert "circuit breaker" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_open_breaker_scraper_never_called(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine(allow=False)
        with patch.object(PublicScraper, "scrape") as mock_scrape:
            await engine.scrape("https://blocked.com/page")
            mock_scrape.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_records_true(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(
            PublicScraper, "scrape", return_value=_success_result("https://good.com/article")
        ):
            await engine.scrape("https://good.com/article")

        engine.circuit_breaker.record.assert_called_once()
        _domain, _success = engine.circuit_breaker.record.call_args[0]
        assert _success is True

    @pytest.mark.asyncio
    async def test_challenge_records_false(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(PublicScraper, "scrape", side_effect=ChallengeError("blocked")):
            await engine.scrape("https://challenge.com/page")

        engine.circuit_breaker.record.assert_called_once()
        _domain, _success = engine.circuit_breaker.record.call_args[0]
        assert _success is False


# ---------------------------------------------------------------------------
# Rate limiter is awaited before dispatch
# ---------------------------------------------------------------------------


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_called_before_scrape(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        call_order: list[str] = []

        original_acquire = engine.rate_limiter.acquire

        async def _tracking_acquire(domain):
            call_order.append("acquire")
            return await original_acquire(domain)

        engine.rate_limiter.acquire = _tracking_acquire

        async def _tracking_scrape(self, url):
            call_order.append("scrape")
            return _success_result(url)

        with patch.object(PublicScraper, "scrape", _tracking_scrape):
            await engine.scrape("https://example.com/page")

        assert call_order.index("acquire") < call_order.index("scrape")


# ---------------------------------------------------------------------------
# Sink integration
# ---------------------------------------------------------------------------


class TestSink:
    @pytest.mark.asyncio
    async def test_sink_write_called_on_success(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_sink = AsyncMock()
        engine = _make_engine(sink=fake_sink)
        with patch.object(PublicScraper, "scrape", return_value=_success_result()):
            await engine.scrape("https://example.com/page")

        fake_sink.write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sink_not_called_on_challenge(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_sink = AsyncMock()
        engine = _make_engine(sink=fake_sink)
        with patch.object(PublicScraper, "scrape", side_effect=ChallengeError("blocked")):
            await engine.scrape("https://example.com/page")

        fake_sink.write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Exception handling — status mapping
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    @pytest.mark.asyncio
    async def test_challenge_error_yields_challenge_status(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(PublicScraper, "scrape", side_effect=ChallengeError("cf")):
            result = await engine.scrape("https://cf.com/page")
        assert result.status == "challenge"

    @pytest.mark.asyncio
    async def test_rate_limit_error_yields_rate_limited_status(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(PublicScraper, "scrape", side_effect=RateLimitError("429")):
            result = await engine.scrape("https://rl.com/page")
        assert result.status == "rate_limited"

    @pytest.mark.asyncio
    async def test_proxy_error_yields_proxy_failed_status(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(PublicScraper, "scrape", side_effect=ProxyError("dead")):
            result = await engine.scrape("https://px.com/page")
        assert result.status == "proxy_failed"

    @pytest.mark.asyncio
    async def test_driver_error_yields_error_status(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(PublicScraper, "scrape", side_effect=DriverError("fail")):
            result = await engine.scrape("https://de.com/page")
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_generic_scrapeforge_error_yields_error_status(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        with patch.object(PublicScraper, "scrape", side_effect=ScrapeForgeError("oops")):
            result = await engine.scrape("https://err.com/page")
        assert result.status == "error"


# ---------------------------------------------------------------------------
# batch_scrape
# ---------------------------------------------------------------------------


class TestBatchScrape:
    @pytest.mark.asyncio
    async def test_returns_list_of_results(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        engine = _make_engine()
        urls = ["https://a.com", "https://b.com"]
        with patch.object(
            PublicScraper,
            "scrape",
            side_effect=[_success_result(u) for u in urls],
        ):
            results = await engine.batch_scrape(urls)
        assert len(results) == 2
        assert all(isinstance(r, ScrapeResult) for r in results)


# ---------------------------------------------------------------------------
# Helper — async return factory for patching
# ---------------------------------------------------------------------------


async def _async_return(value):
    return value
