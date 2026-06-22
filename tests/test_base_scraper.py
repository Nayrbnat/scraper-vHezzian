"""Tests for BaseScraper (SPEC.md §3.7).

TDD first — these tests drive the implementation in scrapeforge/scrapers/base.py.

All tests are hermetic: no live network, no real drivers, no env requirements.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.stealth_bridge import StealthBridge
from scrapeforge.exceptions import ChallengeError

# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

GOOD_HTML = """\
<html><body>
  <h1 class="entry-title">Test Article Title</h1>
  <div class="entry-content">{content}</div>
  <span class="author">Jane Doe</span>
</body></html>
""".format(
    content="This is a great article about scraping. " * 30  # 30*40 = 1200 chars > 500 min
)

SOFT_BLOCK_HTML = """\
<html><head><title>Just a moment...</title></head>
<body><h1>Checking your browser</h1><p>Please wait...</p></body>
</html>
"""


# ---------------------------------------------------------------------------
# Concrete subclass for testing (only BaseScraper is abstract)
# ---------------------------------------------------------------------------


def _make_concrete_scraper(**kwargs: Any):
    """Return an instance of a minimal concrete subclass of BaseScraper."""
    from scrapeforge.scrapers.base import BaseScraper

    class _TestScraper(BaseScraper):
        BUCKET = "test"
        DOMAINS = ["example.com"]
        DEFAULT_DRIVER = "curl_cffi"

        async def scrape(self, url: str) -> ScrapeResult:
            return ScrapeResult(status="success", driver_used="curl_cffi")

        def _get_selectors(self) -> dict:
            return {
                "title": "h1.entry-title",
                "content": "div.entry-content",
                "author": "span.author",
                "publish_date": "time[datetime]",
            }

    return _TestScraper(**kwargs)


# ---------------------------------------------------------------------------
# _create_default_bridge
# ---------------------------------------------------------------------------


class TestCreateDefaultBridge:
    def test_returns_stealth_bridge(self):
        scraper = _make_concrete_scraper()
        bridge = scraper._create_default_bridge(proxy=None)
        assert isinstance(bridge, StealthBridge)

    def test_uses_default_driver(self):
        scraper = _make_concrete_scraper()
        bridge = scraper._create_default_bridge(proxy=None)
        assert bridge.driver == "curl_cffi"

    def test_passes_proxy_through(self):
        scraper = _make_concrete_scraper()
        bridge = scraper._create_default_bridge(proxy="http://proxy:8080")
        assert bridge.proxy == "http://proxy:8080"


# ---------------------------------------------------------------------------
# __init__ — bridge is stored correctly
# ---------------------------------------------------------------------------


class TestInit:
    def test_stores_proxy(self):
        scraper = _make_concrete_scraper(proxy="http://p:1234")
        assert scraper.proxy == "http://p:1234"

    def test_semaphore_bound(self):
        scraper = _make_concrete_scraper(max_concurrency=3)
        assert scraper._semaphore._value == 3  # asyncio.Semaphore internal

    def test_bridge_injected(self):
        fake_bridge = MagicMock(spec=StealthBridge)
        scraper = _make_concrete_scraper(bridge=fake_bridge)
        assert scraper.bridge is fake_bridge

    def test_bridge_is_none_when_not_injected(self):
        """When bridge=None the stored bridge is None; scrape() creates per-call."""
        scraper = _make_concrete_scraper()
        assert scraper.bridge is None


# ---------------------------------------------------------------------------
# _extract_article — happy path
# ---------------------------------------------------------------------------


class TestExtractArticleHappy:
    def test_returns_article(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert isinstance(article, Article)

    def test_url_passed_through(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert article.url == "https://example.com/a"

    def test_title_extracted(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert article.title == "Test Article Title"

    def test_author_extracted(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert article.author == "Jane Doe"

    def test_content_non_empty(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert len(article.content) > 100

    def test_metadata_bucket(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert article.metadata.get("bucket") == "test"

    def test_metadata_source_domain(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert "source_domain" in article.metadata

    def test_publish_date_none(self):
        scraper = _make_concrete_scraper()
        article = scraper._extract_article(GOOD_HTML, "https://example.com/a")
        assert article.publish_date is None


# ---------------------------------------------------------------------------
# _extract_article — soft-block raises ChallengeError
# ---------------------------------------------------------------------------


class TestExtractArticleChallenge:
    def test_soft_block_raises_challenge_error(self):
        scraper = _make_concrete_scraper()
        with pytest.raises(ChallengeError):
            scraper._extract_article(SOFT_BLOCK_HTML, "https://example.com/blocked")


# ---------------------------------------------------------------------------
# batch_scrape
# ---------------------------------------------------------------------------


class TestBatchScrape:
    """batch_scrape gathers results, respects semaphore, writes successes to sink."""

    @pytest.fixture
    def tracking_scraper(self):
        """A scraper whose scrape() tracks concurrency."""
        from scrapeforge.scrapers.base import BaseScraper

        class _TrackingScraper(BaseScraper):
            BUCKET = "test"
            DOMAINS = []
            DEFAULT_DRIVER = "curl_cffi"

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.concurrent_peak: int = 0
                self._current: int = 0
                self._call_count: int = 0
                self._lock = asyncio.Lock()

            async def scrape(self, url: str) -> ScrapeResult:  # type: ignore[override]
                async with self._lock:
                    self._current += 1
                    self._call_count += 1
                    if self._current > self.concurrent_peak:
                        self.concurrent_peak = self._current

                # Yield control so concurrency is observable
                await asyncio.sleep(0)

                async with self._lock:
                    self._current -= 1

                article = Article(
                    url=url,
                    title="title",
                    content="content",
                )
                return ScrapeResult(
                    status="success",
                    driver_used="curl_cffi",
                    article=article,
                )

        return _TrackingScraper(max_concurrency=2)

    @pytest.mark.asyncio
    async def test_returns_all_results(self, tracking_scraper):
        urls = ["https://a.com", "https://b.com", "https://c.com"]
        results = await tracking_scraper.batch_scrape(urls)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_results_are_scrape_results(self, tracking_scraper):
        urls = ["https://a.com", "https://b.com"]
        results = await tracking_scraper.batch_scrape(urls)
        assert all(isinstance(r, ScrapeResult) for r in results)

    @pytest.mark.asyncio
    async def test_semaphore_respected(self, tracking_scraper):
        urls = [f"https://x{i}.com" for i in range(5)]
        await tracking_scraper.batch_scrape(urls)
        assert tracking_scraper.concurrent_peak <= 2

    @pytest.mark.asyncio
    async def test_writes_successes_to_sink(self):
        from scrapeforge.scrapers.base import BaseScraper

        class _SuccessScraper(BaseScraper):
            BUCKET = "test"
            DOMAINS = []
            DEFAULT_DRIVER = "curl_cffi"

            async def scrape(self, url: str) -> ScrapeResult:  # type: ignore[override]
                return ScrapeResult(
                    status="success",
                    driver_used="curl_cffi",
                    article=Article(url=url, title="t", content="c"),
                )

        # seen() is sync per ArticleSink ABC; write() is async.
        fake_sink = MagicMock()
        fake_sink.seen = MagicMock(return_value=False)
        fake_sink.write = AsyncMock()
        scraper = _SuccessScraper(max_concurrency=5)
        urls = ["https://a.com", "https://b.com"]
        await scraper.batch_scrape(urls, sink=fake_sink)
        assert fake_sink.write.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_seen_urls(self):
        from scrapeforge.scrapers.base import BaseScraper

        scraped: list[str] = []

        class _TrackScraper(BaseScraper):
            BUCKET = "test"
            DOMAINS = []
            DEFAULT_DRIVER = "curl_cffi"

            async def scrape(self, url: str) -> ScrapeResult:  # type: ignore[override]
                scraped.append(url)
                return ScrapeResult(status="success", driver_used="curl_cffi")

        # seen() is sync per ArticleSink ABC; write() is async.
        fake_sink = MagicMock()
        fake_sink.seen = MagicMock(side_effect=lambda url: url == "https://b.com")
        fake_sink.write = AsyncMock()

        scraper = _TrackScraper(max_concurrency=5)
        await scraper.batch_scrape(["https://a.com", "https://b.com"], sink=fake_sink)

        assert "https://a.com" in scraped
        assert "https://b.com" not in scraped

    @pytest.mark.asyncio
    async def test_exceptions_captured_not_raised(self):
        """A scraper that raises should not propagate — result has status='error'."""
        from scrapeforge.exceptions import ScrapeForgeError
        from scrapeforge.scrapers.base import BaseScraper

        class _ErrorScraper(BaseScraper):
            BUCKET = "test"
            DOMAINS = []
            DEFAULT_DRIVER = "curl_cffi"

            async def scrape(self, url: str) -> ScrapeResult:  # type: ignore[override]
                raise ScrapeForgeError("boom")

        scraper = _ErrorScraper(max_concurrency=5)
        results = await scraper.batch_scrape(["https://boom.com"])
        assert len(results) == 1
        assert results[0].status == "error"
