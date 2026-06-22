"""Unit tests for SubstackScraper (Bucket 2 — community scraper).

TDD: all tests were written before the implementation.

Strategy
--------
- All I/O is intercepted by a FakeBridge injected at construction time.
  No curl_cffi, no real network.
- The FakeBridge routes responses by URL substring — archive URLs return a
  JSON array of post objects; /posts/<slug> URLs return the matching post object.
- Two archive pages exercise multi-page offset-based pagination:
    - Page 1: 2 public posts + 1 paid post (filtered at archive level)
    - Page 2: 1 public post (short page → pagination stops)
- A single-post fixture exercises the scrape() method.
- Paywall regression test locks the live-verified correction:
    truncated_body_text is NOT a paywall signal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scrapeforge.core.models import Article, ScrapeResult

# ---------------------------------------------------------------------------
# Canned JSON fixtures (realistic Substack API shape per playbook §2)
# ---------------------------------------------------------------------------

# Long enough body HTML to be considered public (> 500 chars cleaned text).
_LONG_BODY_HTML = (
    "<p>"
    + ("This is a sufficiently long body of text that should not be flagged as paywalled. " * 7)
    + "</p>"
)

# Short body HTML — will be flagged as paywalled by the secondary heuristic.
_SHORT_BODY_HTML = "<p>Short snippet.</p>"

# ---------------------------------------------------------------------------
# Archive items (no body_html — used for discovery)
# ---------------------------------------------------------------------------

_ARCHIVE_POST_1 = {
    "id": 111,
    "title": "Hello From Substack",
    "slug": "hello-from-substack",
    "post_date": "2026-06-01T10:00:00.000Z",
    "audience": "everyone",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/hello-from-substack",
    "publishedBylines": [{"id": 1, "name": "Alice Author", "handle": "alice"}],
    "truncated_body_text": "A preview snippet even on public posts.",
}

_ARCHIVE_POST_2_PAID = {
    "id": 222,
    "title": "Premium Exclusive",
    "slug": "premium-exclusive",
    "post_date": "2026-06-02T10:00:00.000Z",
    "audience": "only_paid",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/premium-exclusive",
    "publishedBylines": [{"id": 1, "name": "Alice Author", "handle": "alice"}],
    "truncated_body_text": "Paid teaser.",
}

_ARCHIVE_POST_3 = {
    "id": 333,
    "title": "Another Public Post",
    "slug": "another-public-post",
    "post_date": "2026-06-03T10:00:00.000Z",
    "audience": "everyone",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/another-public-post",
    "publishedBylines": [{"id": 2, "name": "Bob Writer", "handle": "bob"}],
    "truncated_body_text": None,
}

# Page 2 — single item (short page → pagination stops)
_ARCHIVE_POST_4 = {
    "id": 444,
    "title": "Third Public Post",
    "slug": "third-public-post",
    "post_date": "2026-06-04T10:00:00.000Z",
    "audience": "everyone",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/third-public-post",
    "publishedBylines": [{"id": 1, "name": "Alice Author", "handle": "alice"}],
    "truncated_body_text": "Some preview.",
}

# ---------------------------------------------------------------------------
# Single-post objects (have body_html)
# ---------------------------------------------------------------------------

_SINGLE_POST_111 = {
    "id": 111,
    "title": "Hello From Substack",
    "slug": "hello-from-substack",
    "post_date": "2026-06-01T10:00:00.000Z",
    "audience": "everyone",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/hello-from-substack",
    "subtitle": "Welcome post",
    "publishedBylines": [{"id": 1, "name": "Alice Author", "handle": "alice"}],
    "body_html": _LONG_BODY_HTML,
    "truncated_body_text": "A preview snippet even on public posts.",
}

_SINGLE_POST_333 = {
    "id": 333,
    "title": "Another Public Post",
    "slug": "another-public-post",
    "post_date": "2026-06-03T10:00:00.000Z",
    "audience": "everyone",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/another-public-post",
    "subtitle": None,
    "publishedBylines": [{"id": 2, "name": "Bob Writer", "handle": "bob"}],
    "body_html": _LONG_BODY_HTML,
    "truncated_body_text": None,
}

_SINGLE_POST_444 = {
    "id": 444,
    "title": "Third Public Post",
    "slug": "third-public-post",
    "post_date": "2026-06-04T10:00:00.000Z",
    "audience": "everyone",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/third-public-post",
    "subtitle": "Third subtitle",
    "publishedBylines": [{"id": 1, "name": "Alice Author", "handle": "alice"}],
    "body_html": _LONG_BODY_HTML,
    "truncated_body_text": "Some preview.",
}

# A paid single post (body_html is truncated — short).
_SINGLE_POST_PAID = {
    "id": 999,
    "title": "Paid Only Post",
    "slug": "paid-only-post",
    "post_date": "2026-06-05T10:00:00.000Z",
    "audience": "only_paid",
    "type": "newsletter",
    "canonical_url": "https://mypub.substack.com/p/paid-only-post",
    "subtitle": None,
    "publishedBylines": [{"id": 1, "name": "Alice Author", "handle": "alice"}],
    "body_html": _SHORT_BODY_HTML,
    "truncated_body_text": "Paid teaser.",
}

# ---------------------------------------------------------------------------
# Pre-encoded JSON strings for the archive pages
# ---------------------------------------------------------------------------


# Build a "filler" public post factory so page 1 can be exactly 12 items
# (the default SUBSTACK_ARCHIVE_PAGE_SIZE) without committing real content.
def _filler_archive_post(n: int) -> dict:
    return {
        "id": 10000 + n,
        "title": f"Filler Post {n}",
        "slug": f"filler-post-{n}",
        "post_date": "2026-05-01T00:00:00.000Z",
        "audience": "everyone",
        "type": "newsletter",
        "canonical_url": f"https://mypub.substack.com/p/filler-post-{n}",
        "publishedBylines": [{"id": 99, "name": "Filler Author", "handle": "filler"}],
        "truncated_body_text": None,
    }


def _filler_post_body(n: int) -> dict:
    return {
        "id": 10000 + n,
        "title": f"Filler Post {n}",
        "slug": f"filler-post-{n}",
        "post_date": "2026-05-01T00:00:00.000Z",
        "audience": "everyone",
        "type": "newsletter",
        "canonical_url": f"https://mypub.substack.com/p/filler-post-{n}",
        "subtitle": None,
        "publishedBylines": [{"id": 99, "name": "Filler Author", "handle": "filler"}],
        "body_html": _LONG_BODY_HTML,
        "truncated_body_text": None,
    }


# Page 1: exactly 12 items (default page_size) so it looks like a full page
# and triggers a second-page fetch.  Layout:
#   [post_1_public, post_2_paid, post_3_public, filler_0 … filler_8]
#   Total = 12 items; 2 public real posts + 1 paid real post + 9 fillers.
_ARCHIVE_PAGE_1_FULL: list = [_ARCHIVE_POST_1, _ARCHIVE_POST_2_PAID, _ARCHIVE_POST_3] + [
    _filler_archive_post(i) for i in range(9)
]
_ARCHIVE_PAGE_1_JSON: str = json.dumps(_ARCHIVE_PAGE_1_FULL)

# Register filler body responses in _URL_RESPONSES (populated after the dict is defined).
_FILLER_BODY_RESPONSES: dict[str, str] = {
    f"/api/v1/posts/filler-post-{i}": json.dumps(_filler_post_body(i)) for i in range(9)
}

# Archive page 2: [post_4_public] — 1 item (short page → stop)
_ARCHIVE_PAGE_2_JSON: str = json.dumps([_ARCHIVE_POST_4])

# Archive empty page (stop condition)
_ARCHIVE_EMPTY_JSON: str = json.dumps([])

# ---------------------------------------------------------------------------
# URL-routing FakeBridge
# ---------------------------------------------------------------------------

# Maps URL substrings to JSON responses.
_URL_RESPONSES: dict[str, str] = {
    "/api/v1/archive": _ARCHIVE_PAGE_1_JSON,  # default; overridden by router logic
    "/api/v1/posts/hello-from-substack": json.dumps(_SINGLE_POST_111),
    "/api/v1/posts/another-public-post": json.dumps(_SINGLE_POST_333),
    "/api/v1/posts/third-public-post": json.dumps(_SINGLE_POST_444),
    "/api/v1/posts/paid-only-post": json.dumps(_SINGLE_POST_PAID),
    **_FILLER_BODY_RESPONSES,
}


def _make_url_router_bridge(url_map: dict[str, str]) -> Any:
    """Return a FakeBridge that routes responses by URL substring.

    Unlike the Reddit test's sequential FakeBridge, Substack makes interleaved
    calls (archive → post body → archive → post body …), so routing by URL
    substring is the correct approach here.

    ``url_map`` maps URL substrings (checked in insertion order) to the JSON
    response string to return when a ``navigate`` call matches that substring.
    """
    bridge = AsyncMock()
    bridge.driver = "curl_cffi"

    # Track last navigated URL so get_html can return the right response.
    _last_url: list[str] = [""]

    async def _navigate(url: str) -> ScrapeResult:
        _last_url[0] = url
        return ScrapeResult(status="success", driver_used="curl_cffi")

    async def _get_html() -> str:
        url = _last_url[0]
        for substring, response in url_map.items():
            if substring in url:
                return response
        raise ValueError(f"FakeBridge: no route for URL: {url!r}")

    bridge.navigate = _navigate  # type: ignore[assignment]
    bridge.get_html = _get_html  # type: ignore[assignment]
    bridge.__aenter__ = AsyncMock(return_value=bridge)
    bridge.__aexit__ = AsyncMock(return_value=False)
    return bridge


def _make_pagination_bridge(page1: str, page2: str) -> Any:
    """Return a FakeBridge that returns archive pages in order then routes post fetches.

    Archive calls are answered sequentially (page1, then page2, then empty).
    Post-body calls are routed by URL substring.
    """
    _archive_pages = [page1, page2, _ARCHIVE_EMPTY_JSON]
    _archive_call_count: list[int] = [0]
    _last_url: list[str] = [""]

    bridge = AsyncMock()
    bridge.driver = "curl_cffi"

    async def _navigate(url: str) -> ScrapeResult:
        _last_url[0] = url
        return ScrapeResult(status="success", driver_used="curl_cffi")

    async def _get_html() -> str:
        url = _last_url[0]
        if "/api/v1/archive" in url:
            idx = min(_archive_call_count[0], len(_archive_pages) - 1)
            _archive_call_count[0] += 1
            return _archive_pages[idx]
        # Route post body fetches by slug substring.
        for substring, response in _URL_RESPONSES.items():
            if "/api/v1/posts/" in substring and substring in url:
                return response
        raise ValueError(f"FakeBridge: no route for URL: {url!r}")

    bridge.navigate = _navigate  # type: ignore[assignment]
    bridge.get_html = _get_html  # type: ignore[assignment]
    bridge.__aenter__ = AsyncMock(return_value=bridge)
    bridge.__aexit__ = AsyncMock(return_value=False)
    return bridge


# ---------------------------------------------------------------------------
# Import side-effect — ensure registry decorator runs before tests probe it
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_substack_registered():
    """Ensure SubstackScraper is registered in _REGISTRY for every test.

    test_registry.py has an ``autouse`` fixture (``_restore_registry``) that
    snapshots _REGISTRY before each of its own tests and restores it afterward.
    When that file's tests run first, the final restore leaves _REGISTRY at the
    snapshot value it had before ``discover_scrapers()`` was called — usually
    ``{}``.  The ``@register_scraper`` decorator only fires once (at module
    import time), so once the module is in ``sys.modules`` re-importing is a
    no-op.  This function-scoped fixture re-inserts the two substack entries
    before every test in this file so the registry binding tests always see
    them, regardless of test-collection order.
    """
    import scrapeforge.scrapers.community.substack as _ss_mod
    from scrapeforge.core.registry import _REGISTRY

    cls = _ss_mod.SubstackScraper
    _REGISTRY.setdefault("substack.com", cls)
    _REGISTRY.setdefault("www.substack.com", cls)


# ---------------------------------------------------------------------------
# Registry binding
# ---------------------------------------------------------------------------


class TestRegistryBinding:
    def test_substack_com_bound(self):
        from scrapeforge.core.registry import _REGISTRY

        assert "substack.com" in _REGISTRY

    def test_www_substack_com_bound(self):
        from scrapeforge.core.registry import _REGISTRY

        assert "www.substack.com" in _REGISTRY

    def test_bound_to_substack_scraper(self):
        from scrapeforge.core.registry import _REGISTRY
        from scrapeforge.scrapers.community.substack import SubstackScraper

        assert _REGISTRY["substack.com"] is SubstackScraper
        assert _REGISTRY["www.substack.com"] is SubstackScraper


# ---------------------------------------------------------------------------
# Class attributes
# ---------------------------------------------------------------------------


class TestSubstackScraperAttrs:
    def test_bucket(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        assert SubstackScraper.BUCKET == "community"

    def test_default_driver(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        assert SubstackScraper.DEFAULT_DRIVER == "curl_cffi"


# ---------------------------------------------------------------------------
# SubstackSettings defaults
# ---------------------------------------------------------------------------


class TestSubstackSettings:
    def test_use_curl_cffi_default(self, fake_env):
        from scrapeforge.scrapers.community.substack import SubstackSettings

        s = SubstackSettings()
        assert s.SUBSTACK_USE_CURL_CFFI is True

    def test_archive_page_size_default(self, fake_env):
        from scrapeforge.scrapers.community.substack import SubstackSettings

        s = SubstackSettings()
        assert s.SUBSTACK_ARCHIVE_PAGE_SIZE == 12

    def test_public_only_default(self, fake_env):
        from scrapeforge.scrapers.community.substack import SubstackSettings

        s = SubstackSettings()
        assert s.SUBSTACK_PUBLIC_ONLY is True


# ---------------------------------------------------------------------------
# _normalize_base — URL normalisation
# ---------------------------------------------------------------------------


class TestNormalizeBase:
    @pytest.fixture
    def scraper(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        return SubstackScraper()

    def test_bare_subdomain_gets_substack_com(self, scraper):
        """'mypub' → 'https://mypub.substack.com'"""
        assert scraper._normalize_base("mypub") == "https://mypub.substack.com"

    def test_bare_subdomain_no_dot(self, scraper):
        """A subdomain without dots should get the .substack.com suffix."""
        result = scraper._normalize_base("semianalysis")
        assert result == "https://semianalysis.substack.com"

    def test_full_custom_domain(self, scraper):
        """'www.noahpinion.blog' → 'https://www.noahpinion.blog'"""
        assert scraper._normalize_base("www.noahpinion.blog") == "https://www.noahpinion.blog"

    def test_full_substack_subdomain(self, scraper):
        """'garymarcus.substack.com' → 'https://garymarcus.substack.com'"""
        result = scraper._normalize_base("garymarcus.substack.com")
        assert result == "https://garymarcus.substack.com"

    def test_full_url_with_scheme(self, scraper):
        """'https://astral.substack.com' → 'https://astral.substack.com' (no double-https)"""
        result = scraper._normalize_base("https://astral.substack.com")
        assert result == "https://astral.substack.com"

    def test_trailing_slash_stripped(self, scraper):
        """Trailing slash must be stripped from the result."""
        result = scraper._normalize_base("https://mypub.substack.com/")
        assert result == "https://mypub.substack.com"

    def test_custom_domain_no_scheme(self, scraper):
        """Custom domain without scheme but with dots → add https://"""
        result = scraper._normalize_base("www.noahpinion.blog")
        assert result == "https://www.noahpinion.blog"


# ---------------------------------------------------------------------------
# _is_paywalled — paywall detection
# ---------------------------------------------------------------------------


class TestIsPaywalled:
    @pytest.fixture
    def scraper(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        return SubstackScraper()

    def test_audience_only_paid_is_paywalled(self, scraper):
        post = {"audience": "only_paid", "body_html": _LONG_BODY_HTML}
        assert scraper._is_paywalled(post) is True

    def test_audience_everyone_long_body_not_paywalled(self, scraper):
        post = {"audience": "everyone", "body_html": _LONG_BODY_HTML}
        assert scraper._is_paywalled(post) is False

    def test_audience_everyone_short_body_is_paywalled(self, scraper):
        """Secondary heuristic: < 500 chars cleaned text → paywalled."""
        post = {"audience": "everyone", "body_html": _SHORT_BODY_HTML}
        assert scraper._is_paywalled(post) is True

    def test_missing_body_html_is_paywalled(self, scraper):
        """No body_html at all → no content → paywalled by secondary heuristic."""
        post = {"audience": "everyone", "body_html": None}
        assert scraper._is_paywalled(post) is True

    def test_regression_truncated_body_text_not_paywall_signal(self, scraper):
        """REGRESSION: a public post with non-null truncated_body_text must NOT be paywalled.

        Live data shows truncated_body_text is present on PUBLIC posts (playbook §3).
        This test locks the live-verified correction so no one accidentally reverts it.
        """
        post = {
            "audience": "everyone",
            "body_html": _LONG_BODY_HTML,
            "truncated_body_text": "A non-null preview snippet on a public post.",
        }
        assert scraper._is_paywalled(post) is False, (
            "truncated_body_text must NOT be used as a paywall signal (live-verified correction)"
        )


# ---------------------------------------------------------------------------
# _build_article — field mapping
# ---------------------------------------------------------------------------


class TestBuildArticle:
    @pytest.fixture
    def scraper(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        return SubstackScraper()

    def test_url_from_canonical_url(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.url == "https://mypub.substack.com/p/hello-from-substack"

    def test_title_mapped(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.title == "Hello From Substack"

    def test_author_from_published_bylines(self, scraper):
        """author comes from publishedBylines[0].name"""
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.author == "Alice Author"

    def test_author_missing_bylines(self, scraper):
        """No publishedBylines → author is None."""
        post = dict(_SINGLE_POST_111)
        post["publishedBylines"] = []
        article = scraper._build_article(post)
        assert article.author is None

    def test_publish_date_tz_aware(self, scraper):
        """post_date ISO-8601 UTC → tz-aware datetime."""
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.publish_date is not None
        assert article.publish_date.tzinfo is not None

    def test_publish_date_correct_value(self, scraper):
        """post_date '2026-06-01T10:00:00.000Z' → UTC 2026-06-01 10:00:00"""
        article = scraper._build_article(_SINGLE_POST_111)
        expected = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        assert article.publish_date == expected

    def test_raw_html_set(self, scraper):
        """raw_html must equal the post's body_html."""
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.raw_html == _LONG_BODY_HTML

    def test_content_is_cleaned_text(self, scraper):
        """content must be plain text extracted from body_html (not raw HTML)."""
        article = scraper._build_article(_SINGLE_POST_111)
        assert "<p>" not in article.content
        assert len(article.content) > 0

    def test_metadata_source_domain(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.metadata["source_domain"] == "mypub.substack.com"

    def test_metadata_bucket(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.metadata["bucket"] == "community"

    def test_metadata_substack_id(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.metadata["substack_id"] == 111

    def test_metadata_audience(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.metadata["audience"] == "everyone"

    def test_metadata_subtitle(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.metadata["subtitle"] == "Welcome post"

    def test_metadata_post_type(self, scraper):
        article = scraper._build_article(_SINGLE_POST_111)
        assert article.metadata["post_type"] == "newsletter"

    def test_returns_article_instance(self, scraper):
        result = scraper._build_article(_SINGLE_POST_111)
        assert isinstance(result, Article)

    def test_missing_post_date_handled(self, scraper):
        """If post_date is absent/None, publish_date should be None (no crash)."""
        post = dict(_SINGLE_POST_111)
        post["post_date"] = None
        article = scraper._build_article(post)
        assert article.publish_date is None


# ---------------------------------------------------------------------------
# scrape_publication — pagination + paywall filtering
# ---------------------------------------------------------------------------


class TestScrapePublication:
    @pytest.mark.asyncio
    async def test_two_pages_paginates_correctly(self):
        """Page 1 (12 archive items, full page) + Page 2 (1 item, short page) → stops after page 2.

        Page 1 layout: 2 real public posts + 1 paid post (skipped) + 9 filler public posts = 12
        items total.  Because 12 == page_size (default), the scraper fetches page 2.
        Page 2: 1 public post (1 < 12 → short page → stop).
        Total success = 11 (page 1) + 1 (page 2) = 12 articles.
        """
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_PAGE_2_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=50)
        # 11 public posts from page 1 (paid post skipped) + 1 from page 2 = 12 total.
        assert len(results) == 12

    @pytest.mark.asyncio
    async def test_paid_archive_item_skipped_no_body_fetch(self):
        """archive items with audience=='only_paid' must be skipped; no body fetch."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_EMPTY_JSON)
        scraper = SubstackScraper(bridge=bridge)

        # Collect all navigate calls to check we never fetched the paid post's body.
        navigated: list[str] = []
        original_navigate = bridge.navigate

        async def _capturing_navigate(url: str) -> ScrapeResult:
            navigated.append(url)
            return await original_navigate(url)

        bridge.navigate = _capturing_navigate

        await scraper.scrape_publication("mypub", limit=50)

        # Confirm the paid slug was never fetched.
        assert not any("premium-exclusive" in u for u in navigated), (
            "paid archive post body must NOT be fetched"
        )

    @pytest.mark.asyncio
    async def test_limit_respected(self):
        """limit=1 must cap results at 1 even if more are available."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_PAGE_2_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=1)

        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_all_success_results(self):
        """All returned results must have status='success'."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_PAGE_2_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=50)

        assert all(r.status == "success" for r in results)

    @pytest.mark.asyncio
    async def test_articles_attached(self):
        """Every success result must have a non-None article."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_PAGE_2_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=50)

        assert all(r.article is not None for r in results)

    @pytest.mark.asyncio
    async def test_returns_list_of_scrape_results(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_PAGE_2_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=50)

        assert isinstance(results, list)
        assert all(isinstance(r, ScrapeResult) for r in results)

    @pytest.mark.asyncio
    async def test_driver_used_is_curl_cffi(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_PAGE_2_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=50)

        assert all(r.driver_used == "curl_cffi" for r in results)

    @pytest.mark.asyncio
    async def test_empty_first_page_returns_empty_list(self):
        """An empty archive page 1 returns [] without error."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_EMPTY_JSON, _ARCHIVE_EMPTY_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=50)

        assert results == []

    @pytest.mark.asyncio
    async def test_stops_on_empty_page(self):
        """Pagination must stop when archive returns an empty array.

        Page 1 (12 items): 11 public + 1 paid (skipped) → 11 articles.
        Page 2 is empty → loop stops.
        """
        from scrapeforge.scrapers.community.substack import SubstackScraper

        # Page 1 is full (12 items), page 2 is empty → loop stops after page 2 attempt.
        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_EMPTY_JSON)
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=50)

        # Page 1: 12 items, 1 paid (skipped) → 11 public posts fetched.
        assert len(results) == 11

    @pytest.mark.asyncio
    async def test_offset_advances_by_items_returned(self):
        """The archive URL for page 2 must include offset equal to items returned by page 1.

        Page 1 has 12 items (full page) → offset for page 2 must be 12.
        """
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_1_JSON, _ARCHIVE_EMPTY_JSON)
        scraper = SubstackScraper(bridge=bridge)

        navigated: list[str] = []
        original_navigate = bridge.navigate

        async def _capturing_navigate(url: str) -> ScrapeResult:
            navigated.append(url)
            return await original_navigate(url)

        bridge.navigate = _capturing_navigate

        await scraper.scrape_publication("mypub", limit=50)

        # Find second archive call (the one after the first page).
        archive_calls = [u for u in navigated if "/api/v1/archive" in u]
        assert len(archive_calls) >= 2, "should have made at least 2 archive calls"
        second_archive_call = archive_calls[1]
        # Page 1 returned 12 items (full page_size) → offset for page 2 must be 12.
        assert "offset=12" in second_archive_call, (
            f"offset should be 12 (items returned by page 1), got: {second_archive_call!r}"
        )

    @pytest.mark.asyncio
    async def test_normalize_bare_subdomain_in_scrape_publication(self):
        """scrape_publication('mypub') must build the archive URL for mypub.substack.com."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_EMPTY_JSON, _ARCHIVE_EMPTY_JSON)
        scraper = SubstackScraper(bridge=bridge)

        navigated: list[str] = []
        original_navigate = bridge.navigate

        async def _capturing_navigate(url: str) -> ScrapeResult:
            navigated.append(url)
            return await original_navigate(url)

        bridge.navigate = _capturing_navigate

        await scraper.scrape_publication("mypub", limit=50)

        assert any("mypub.substack.com" in u for u in navigated)


# ---------------------------------------------------------------------------
# scrape() — single post URL
# ---------------------------------------------------------------------------


class TestScrapeMethod:
    @pytest.mark.asyncio
    async def test_public_post_returns_success(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_url_router_bridge(_URL_RESPONSES)
        scraper = SubstackScraper(bridge=bridge)

        result = await scraper.scrape("https://mypub.substack.com/p/hello-from-substack")

        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_public_post_article_attached(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_url_router_bridge(_URL_RESPONSES)
        scraper = SubstackScraper(bridge=bridge)

        result = await scraper.scrape("https://mypub.substack.com/p/hello-from-substack")

        assert result.article is not None
        assert result.article.title == "Hello From Substack"

    @pytest.mark.asyncio
    async def test_public_post_driver_used(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_url_router_bridge(_URL_RESPONSES)
        scraper = SubstackScraper(bridge=bridge)

        result = await scraper.scrape("https://mypub.substack.com/p/hello-from-substack")

        assert result.driver_used == "curl_cffi"

    @pytest.mark.asyncio
    async def test_paid_post_returns_error_status(self):
        """A paywalled single post → status='error', error='paywalled'."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_url_router_bridge(_URL_RESPONSES)
        scraper = SubstackScraper(bridge=bridge)

        result = await scraper.scrape("https://mypub.substack.com/p/paid-only-post")

        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_paid_post_error_message(self):
        """A paywalled post must set error='paywalled'."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_url_router_bridge(_URL_RESPONSES)
        scraper = SubstackScraper(bridge=bridge)

        result = await scraper.scrape("https://mypub.substack.com/p/paid-only-post")

        assert result.error == "paywalled"

    @pytest.mark.asyncio
    async def test_scrape_builds_correct_api_url(self):
        """scrape() must derive /api/v1/posts/<slug> from the /p/<slug> URL."""
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_url_router_bridge(_URL_RESPONSES)
        scraper = SubstackScraper(bridge=bridge)

        navigated: list[str] = []
        original_navigate = bridge.navigate

        async def _capturing_navigate(url: str) -> ScrapeResult:
            navigated.append(url)
            return await original_navigate(url)

        bridge.navigate = _capturing_navigate

        await scraper.scrape("https://mypub.substack.com/p/hello-from-substack")

        assert any("/api/v1/posts/hello-from-substack" in u for u in navigated)

    @pytest.mark.asyncio
    async def test_returns_scrape_result_instance(self):
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_url_router_bridge(_URL_RESPONSES)
        scraper = SubstackScraper(bridge=bridge)

        result = await scraper.scrape("https://mypub.substack.com/p/hello-from-substack")

        assert isinstance(result, ScrapeResult)


# ---------------------------------------------------------------------------
# Robustness — unexpected payload shapes (playbook §6: handle defensively)
# ---------------------------------------------------------------------------


def _make_fixed_bridge(response_json: str) -> Any:
    """Return a FakeBridge that answers EVERY navigate with the same JSON body."""
    bridge = AsyncMock()
    bridge.driver = "curl_cffi"

    async def _navigate(url: str) -> ScrapeResult:
        return ScrapeResult(status="success", driver_used="curl_cffi")

    async def _get_html() -> str:
        return response_json

    bridge.navigate = _navigate  # type: ignore[assignment]
    bridge.get_html = _get_html  # type: ignore[assignment]
    bridge.__aenter__ = AsyncMock(return_value=bridge)
    bridge.__aexit__ = AsyncMock(return_value=False)
    return bridge


class TestUnexpectedPayloads:
    @pytest.mark.asyncio
    async def test_archive_error_envelope_stops_gracefully(self):
        """If the archive endpoint returns a dict (error envelope) not a list,

        scrape_publication must stop and return [] — never raise AttributeError
        from iterating a dict's keys (review finding #2).
        """
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_fixed_bridge(json.dumps({"error": "rate limited"}))
        scraper = SubstackScraper(bridge=bridge)

        results = await scraper.scrape_publication("mypub", limit=10)

        assert results == []

    @pytest.mark.asyncio
    async def test_single_post_non_dict_payload_returns_error(self):
        """scrape() must return status='error' (not crash) if the post endpoint

        returns a non-dict payload (e.g. a list or error envelope).
        """
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_fixed_bridge(json.dumps(["unexpected", "array"]))
        scraper = SubstackScraper(bridge=bridge)

        result = await scraper.scrape("https://mypub.substack.com/p/whatever")

        assert result.status == "error"
        assert result.article is None


# ---------------------------------------------------------------------------
# Pagination — a FULL page of paid posts must still advance the offset
# ---------------------------------------------------------------------------


# 12 paid items (a full page) — all should be skipped at the archive pre-filter.
_ARCHIVE_PAGE_ALL_PAID: list = [
    {
        "id": 50000 + n,
        "title": f"Paid Post {n}",
        "slug": f"paid-post-{n}",
        "post_date": "2026-04-01T00:00:00.000Z",
        "audience": "only_paid",
        "type": "newsletter",
        "canonical_url": f"https://mypub.substack.com/p/paid-post-{n}",
        "publishedBylines": [{"id": 7, "name": "Paid Author", "handle": "paid"}],
        "truncated_body_text": "teaser",
    }
    for n in range(12)
]
_ARCHIVE_PAGE_ALL_PAID_JSON: str = json.dumps(_ARCHIVE_PAGE_ALL_PAID)


class TestAllPaidPageAdvances:
    @pytest.mark.asyncio
    async def test_full_paid_page_advances_to_next_page(self):
        """A full page where every item is paid must NOT terminate the loop.

        Page 1: 12 paid items (all skipped at pre-filter, no body fetched).
        Page 2: 1 public post (short page → stop).
        Expected: 1 article (only the page-2 public post), proving the offset
        advanced past the full paid page rather than stopping or looping forever
        (review finding #4).
        """
        from scrapeforge.scrapers.community.substack import SubstackScraper

        bridge = _make_pagination_bridge(_ARCHIVE_PAGE_ALL_PAID_JSON, _ARCHIVE_PAGE_2_JSON)
        scraper = SubstackScraper(bridge=bridge)

        navigated: list[str] = []
        original_navigate = bridge.navigate

        async def _capturing_navigate(url: str) -> ScrapeResult:
            navigated.append(url)
            return await original_navigate(url)

        bridge.navigate = _capturing_navigate

        results = await scraper.scrape_publication("mypub", limit=50)

        # Only the page-2 public post survives.
        assert len(results) == 1
        assert results[0].article.title == "Third Public Post"
        # No paid body was ever fetched.
        assert not any("/p/paid-post-" in u for u in navigated)
        # The second archive page was fetched with offset advanced by the 12
        # items returned on page 1 (not page_size-blind, not stuck at 0).
        archive_calls = [u for u in navigated if "/api/v1/archive" in u]
        assert any("offset=12" in u for u in archive_calls)


# ---------------------------------------------------------------------------
# Custom-domain registration (Invariant #16 — review finding #1)
# ---------------------------------------------------------------------------


class TestCustomDomainRegistration:
    def test_custom_domains_empty_by_default(self, fake_env):
        from scrapeforge.scrapers.community.substack import SubstackSettings

        assert SubstackSettings().custom_domains() == []

    def test_custom_domains_parses_csv(self, fake_env):
        from scrapeforge.scrapers.community.substack import SubstackSettings

        s = SubstackSettings(SUBSTACK_CUSTOM_DOMAINS="www.noahpinion.blog, example.com ,")
        # Whitespace trimmed, blank entries dropped.
        assert s.custom_domains() == ["www.noahpinion.blog", "example.com"]

    def test_custom_domain_registration_routes_to_substack(self, fake_env):
        """Registering a custom domain makes the engine resolve it to SubstackScraper.

        This is the mechanism the module-level registration loop uses at import
        time when SUBSTACK_CUSTOM_DOMAINS is set (review finding #1).
        """
        from scrapeforge.core.registry import _REGISTRY, get_scraper_for, register_scraper
        from scrapeforge.scrapers.community.substack import SubstackScraper

        domain = "custom-substack-test.example"
        try:
            register_scraper(domain)(SubstackScraper)
            assert get_scraper_for(domain) is SubstackScraper
            # Sub-host of the custom domain resolves via suffix match too.
            assert get_scraper_for(f"www.{domain}") is SubstackScraper
        finally:
            _REGISTRY.pop(domain, None)
