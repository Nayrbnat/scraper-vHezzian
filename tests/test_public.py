"""Tests for PublicScraper (SPEC.md §3.10).

TDD first — tests drive scrapeforge/scrapers/public/public.py.

All tests use injected fake bridges — no live network, no real drivers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.exceptions import ChallengeError

# ---------------------------------------------------------------------------
# HTML fixtures (same set used in base tests; repeated here for independence)
# ---------------------------------------------------------------------------

_CONTENT = "This is article content that is definitely longer than five hundred characters. " * 10

GOOD_HTML = f"""\
<html><body>
  <h1 class="entry-title">Public Article</h1>
  <article>{_CONTENT}</article>
  <span class="author">Bob Smith</span>
</body></html>
"""

SOFT_BLOCK_HTML = """\
<html><head><title>Just a moment...</title></head>
<body><p>Checking your browser before accessing.</p></body>
</html>
"""


# ---------------------------------------------------------------------------
# Fake StealthBridge that acts as an async context manager
# ---------------------------------------------------------------------------


def _make_fake_bridge(html: str = GOOD_HTML) -> AsyncMock:
    """Return an async context manager mock that yields itself and has .driver."""
    bridge = AsyncMock()
    bridge.driver = "curl_cffi"
    bridge.navigate = AsyncMock(
        return_value=ScrapeResult(status="success", driver_used="curl_cffi")
    )
    bridge.get_html = AsyncMock(return_value=html)

    # Make it work as `async with bridge as b:` where b == bridge
    bridge.__aenter__ = AsyncMock(return_value=bridge)
    bridge.__aexit__ = AsyncMock(return_value=False)
    return bridge


# ---------------------------------------------------------------------------
# PublicScraper class attributes
# ---------------------------------------------------------------------------


class TestPublicScraperAttrs:
    def test_bucket(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        assert PublicScraper.BUCKET == "public"

    def test_default_driver(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        assert PublicScraper.DEFAULT_DRIVER == "curl_cffi"

    def test_requires_auth_false(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        assert PublicScraper.REQUIRES_AUTH is False


# ---------------------------------------------------------------------------
# _get_selectors
# ---------------------------------------------------------------------------


class TestGetSelectors:
    def test_has_title(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        s = PublicScraper()._get_selectors()
        assert "title" in s

    def test_has_content(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        s = PublicScraper()._get_selectors()
        assert "content" in s

    def test_has_author(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        s = PublicScraper()._get_selectors()
        assert "author" in s

    def test_has_publish_date(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        s = PublicScraper()._get_selectors()
        assert "publish_date" in s


# ---------------------------------------------------------------------------
# scrape() — happy path
# ---------------------------------------------------------------------------


class TestPublicScrapeScrape:
    @pytest.mark.asyncio
    async def test_returns_success_status(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(GOOD_HTML)
        scraper = PublicScraper(bridge=fake_bridge)
        result = await scraper.scrape("https://example.com/article")
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_result_has_article(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(GOOD_HTML)
        scraper = PublicScraper(bridge=fake_bridge)
        result = await scraper.scrape("https://example.com/article")
        assert result.article is not None
        assert isinstance(result.article, Article)

    @pytest.mark.asyncio
    async def test_article_title_extracted(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(GOOD_HTML)
        scraper = PublicScraper(bridge=fake_bridge)
        result = await scraper.scrape("https://example.com/article")
        assert result.article is not None
        assert "Public Article" in result.article.title

    @pytest.mark.asyncio
    async def test_driver_used_set(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(GOOD_HTML)
        scraper = PublicScraper(bridge=fake_bridge)
        result = await scraper.scrape("https://example.com/article")
        assert result.driver_used == "curl_cffi"

    @pytest.mark.asyncio
    async def test_navigate_called(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(GOOD_HTML)
        scraper = PublicScraper(bridge=fake_bridge)
        await scraper.scrape("https://example.com/article")
        fake_bridge.navigate.assert_awaited_once_with("https://example.com/article")

    @pytest.mark.asyncio
    async def test_proxy_used_passed(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(GOOD_HTML)
        scraper = PublicScraper(bridge=fake_bridge, proxy="http://px:8080")
        result = await scraper.scrape("https://example.com/article")
        assert result.proxy_used == "http://px:8080"


# ---------------------------------------------------------------------------
# scrape() — soft-block page propagates ChallengeError
# ---------------------------------------------------------------------------


class TestPublicScrapeChallenge:
    @pytest.mark.asyncio
    async def test_challenge_propagates(self):
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(SOFT_BLOCK_HTML)
        scraper = PublicScraper(bridge=fake_bridge)
        with pytest.raises(ChallengeError):
            await scraper.scrape("https://blocked.com/page")


# ---------------------------------------------------------------------------
# batch_scrape — each URL gets its own bridge (no session sharing)
# ---------------------------------------------------------------------------


class TestPublicScraperBatchBridgeIsolation:
    @pytest.mark.asyncio
    async def test_each_url_gets_distinct_bridge(self):
        """batch_scrape with no injected bridge must create a FRESH bridge per URL.

        Concurrent URLs must NOT share one backend session — violates Invariant #7.
        We monkeypatch _create_default_bridge so each call returns a NEW distinct fake.
        """
        from scrapeforge.scrapers.public.public import PublicScraper

        created_bridges: list[AsyncMock] = []

        def _fresh_bridge(_proxy):
            b = _make_fake_bridge(GOOD_HTML)
            created_bridges.append(b)
            return b

        scraper = PublicScraper()  # no injected bridge → self.bridge is None
        urls = ["https://a.com/p", "https://b.com/p", "https://c.com/p"]

        with patch.object(scraper, "_create_default_bridge", side_effect=_fresh_bridge):
            results = await scraper.batch_scrape(urls)

        # One distinct bridge per URL.
        assert len(created_bridges) == 3
        assert len({id(b) for b in created_bridges}) == 3

        # All calls succeeded.
        assert all(r.status == "success" for r in results)

    @pytest.mark.asyncio
    async def test_injected_bridge_reused_across_calls(self):
        """An explicitly injected bridge IS shared — it is a test override / single-call use."""
        from scrapeforge.scrapers.public.public import PublicScraper

        fake_bridge = _make_fake_bridge(GOOD_HTML)
        scraper = PublicScraper(bridge=fake_bridge)

        # When a bridge is injected, scrape() uses it directly (test-override contract).
        result = await scraper.scrape("https://example.com/p")
        assert result.status == "success"
        fake_bridge.__aenter__.assert_awaited_once()
