"""Unit tests for RedditScraper (U8 — Bucket 2 community scraper).

TDD: all tests were written before the implementation.

Strategy
--------
- All I/O is intercepted by a FAKE bridge (``FakeBridge``) injected at construction
  time.  No curl_cffi, no real network.
- The fake bridge is an async context manager whose ``navigate`` returns a success
  ``ScrapeResult`` and whose ``get_html`` returns a pre-baked JSON string.
- Two captured listing pages exercise multi-page pagination:
    - Page 1: 2 posts + ``after`` cursor
    - Page 2: 1 post + ``after: null`` (end of listing)
- A single-post fixture exercises the 2-element comments array.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scrapeforge.core.models import Article, ScrapeResult

# ---------------------------------------------------------------------------
# Canned JSON fixtures (realistic Reddit API shape per playbook §2)
# ---------------------------------------------------------------------------

# Two self posts + one link post spread across two pages.
# Page 1 has 2 children (one self-post, one link-post) and an ``after`` cursor.
# Page 2 has 1 child (another self-post) and ``after: null``.

_POST_1 = {
    "kind": "t3",
    "data": {
        "id": "abc111",
        "name": "t3_abc111",
        "title": "First Python Post",
        "selftext": "This is the body of the first post.",
        "url": "https://www.reddit.com/r/Python/comments/abc111/first_python_post/",
        "permalink": "/r/Python/comments/abc111/first_python_post/",
        "author": "user_alice",
        "created_utc": 1_700_000_000.0,
        "subreddit": "Python",
        "score": 42,
        "num_comments": 7,
        "is_self": True,
        "over_18": False,
    },
}

# Link post — selftext is empty; url points to an external site.
_POST_2 = {
    "kind": "t3",
    "data": {
        "id": "def222",
        "name": "t3_def222",
        "title": "Cool Python Tool",
        "selftext": "",
        "url": "https://external.example.com/cool-tool",
        "permalink": "/r/Python/comments/def222/cool_python_tool/",
        "author": "user_bob",
        "created_utc": 1_700_001_000.0,
        "subreddit": "Python",
        "score": 100,
        "num_comments": 20,
        "is_self": False,
        "over_18": False,
    },
}

_POST_3 = {
    "kind": "t3",
    "data": {
        "id": "ghi333",
        "name": "t3_ghi333",
        "title": "Third Python Post",
        "selftext": "Body of post three.",
        "url": "https://www.reddit.com/r/Python/comments/ghi333/third_python_post/",
        "permalink": "/r/Python/comments/ghi333/third_python_post/",
        "author": "user_carol",
        "created_utc": 1_700_002_000.0,
        "subreddit": "Python",
        "score": 5,
        "num_comments": 1,
        "is_self": True,
        "over_18": False,
    },
}

# A non-t3 child to verify skipping.
_COMMENT_CHILD = {
    "kind": "t1",
    "data": {
        "id": "zzz999",
        "body": "some comment",
        "author": "commenter",
    },
}

# Listing page 1 — includes an ``after`` cursor.
_PAGE_1_JSON: str = json.dumps(
    {
        "kind": "Listing",
        "data": {
            "dist": 2,
            "after": "t3_def222",
            "before": None,
            "children": [_POST_1, _COMMENT_CHILD, _POST_2],
        },
    }
)

# Listing page 2 — ``after`` is null (end of listing).
_PAGE_2_JSON: str = json.dumps(
    {
        "kind": "Listing",
        "data": {
            "dist": 1,
            "after": None,
            "before": "t3_abc111",
            "children": [_POST_3],
        },
    }
)

# Single-post 2-element array (comments endpoint).
_SINGLE_POST_JSON: str = json.dumps(
    [
        {
            "kind": "Listing",
            "data": {
                "dist": 1,
                "after": None,
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "xyz999",
                            "name": "t3_xyz999",
                            "title": "A Single Post",
                            "selftext": "Single post body.",
                            "url": "https://www.reddit.com/r/Python/comments/xyz999/a_single_post/",
                            "permalink": "/r/Python/comments/xyz999/a_single_post/",
                            "author": "user_dave",
                            "created_utc": 1_700_003_000.0,
                            "subreddit": "Python",
                            "score": 77,
                            "num_comments": 3,
                            "is_self": True,
                            "over_18": False,
                        },
                    }
                ],
            },
        },
        {
            "kind": "Listing",
            "data": {
                "dist": 0,
                "after": None,
                "children": [],
            },
        },
    ]
)


# ---------------------------------------------------------------------------
# Fake bridge (async context manager)
# ---------------------------------------------------------------------------


def _make_fake_bridge(*pages: str) -> Any:
    """Return a fake bridge that yields successive JSON pages on get_html().

    The bridge is both an async context manager and provides the navigate/get_html
    protocol expected by ``_fetch_json``.  Each call to ``get_html`` returns the next
    page in sequence; the last page is returned for any subsequent calls.
    """
    bridge = AsyncMock()
    bridge.driver = "curl_cffi"

    # navigate always succeeds.
    bridge.navigate = AsyncMock(
        return_value=ScrapeResult(status="success", driver_used="curl_cffi")
    )

    # get_html cycles through the provided pages.
    page_list = list(pages)
    call_count = [0]

    async def _get_html() -> str:
        idx = min(call_count[0], len(page_list) - 1)
        call_count[0] += 1
        return page_list[idx]

    bridge.get_html = _get_html  # type: ignore[assignment]

    # Async context manager support — yields itself.
    bridge.__aenter__ = AsyncMock(return_value=bridge)
    bridge.__aexit__ = AsyncMock(return_value=False)
    return bridge


# ---------------------------------------------------------------------------
# Import side effect — make sure registry decorator runs before tests probe it
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _import_reddit_module():
    """Importing the module registers 'reddit.com' and 'www.reddit.com' in the registry."""
    import scrapeforge.scrapers.community.reddit  # noqa: F401


# ---------------------------------------------------------------------------
# Registry binding
# ---------------------------------------------------------------------------


class TestRegistryBinding:
    def test_reddit_com_bound(self):
        from scrapeforge.core.registry import _REGISTRY

        assert "reddit.com" in _REGISTRY

    def test_www_reddit_com_bound(self):
        from scrapeforge.core.registry import _REGISTRY

        assert "www.reddit.com" in _REGISTRY

    def test_bound_to_reddit_scraper(self):
        from scrapeforge.core.registry import _REGISTRY
        from scrapeforge.scrapers.community.reddit import RedditScraper

        assert _REGISTRY["reddit.com"] is RedditScraper
        assert _REGISTRY["www.reddit.com"] is RedditScraper


# ---------------------------------------------------------------------------
# Class attributes
# ---------------------------------------------------------------------------


class TestRedditScraperAttrs:
    def test_bucket(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        assert RedditScraper.BUCKET == "community"

    def test_default_driver(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        assert RedditScraper.DEFAULT_DRIVER == "curl_cffi"

    def test_domains(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        assert "reddit.com" in RedditScraper.DOMAINS
        assert "www.reddit.com" in RedditScraper.DOMAINS


# ---------------------------------------------------------------------------
# RedditSettings defaults
# ---------------------------------------------------------------------------


class TestRedditSettings:
    def test_use_json_api_default(self, fake_env):
        from scrapeforge.scrapers.community.reddit import RedditSettings

        s = RedditSettings()
        assert s.REDDIT_USE_JSON_API is True

    def test_json_limit_default(self, fake_env):
        from scrapeforge.scrapers.community.reddit import RedditSettings

        s = RedditSettings()
        assert s.REDDIT_JSON_LIMIT == 100


# ---------------------------------------------------------------------------
# _parse_post — field mapping (per playbook §2.2)
# ---------------------------------------------------------------------------


class TestParsePost:
    @pytest.fixture
    def scraper(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        return RedditScraper()

    def test_title_mapped(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.title == "First Python Post"

    def test_content_mapped_self_post(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.content == "This is the body of the first post."

    def test_content_empty_link_post(self, scraper):
        article = scraper._parse_post(_POST_2["data"])
        assert article.content == ""

    def test_author_mapped(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.author == "user_alice"

    def test_publish_date_utc(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        expected = datetime.fromtimestamp(1_700_000_000.0, tz=UTC)
        assert article.publish_date == expected

    def test_publish_date_is_utc_aware(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.publish_date is not None
        assert article.publish_date.tzinfo is not None

    def test_url_is_absolute(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.url == "https://www.reddit.com/r/Python/comments/abc111/first_python_post/"

    def test_metadata_source_domain(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.metadata["source_domain"] == "reddit.com"

    def test_metadata_bucket(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.metadata["bucket"] == "community"

    def test_metadata_subreddit(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.metadata["subreddit"] == "Python"

    def test_metadata_score(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.metadata["score"] == 42

    def test_metadata_num_comments(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.metadata["num_comments"] == 7

    def test_metadata_reddit_id(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert article.metadata["reddit_id"] == "abc111"

    def test_link_url_in_metadata_for_link_post(self, scraper):
        article = scraper._parse_post(_POST_2["data"])
        assert article.metadata["link_url"] == "https://external.example.com/cool-tool"

    def test_no_link_url_for_self_post(self, scraper):
        article = scraper._parse_post(_POST_1["data"])
        assert "link_url" not in article.metadata

    def test_returns_article_instance(self, scraper):
        result = scraper._parse_post(_POST_1["data"])
        assert isinstance(result, Article)

    def test_removed_selftext_blanked(self, scraper):
        """'[removed]' selftext must be normalised to empty string."""
        data = dict(_POST_1["data"])
        data["selftext"] = "[removed]"
        article = scraper._parse_post(data)
        assert article.content == ""

    def test_deleted_selftext_blanked(self, scraper):
        """'[deleted]' selftext must be normalised to empty string."""
        data = dict(_POST_1["data"])
        data["selftext"] = "[deleted]"
        article = scraper._parse_post(data)
        assert article.content == ""


# ---------------------------------------------------------------------------
# scrape_subreddit — pagination with fake bridge
# ---------------------------------------------------------------------------


class TestScrapeSubreddit:
    @pytest.mark.asyncio
    async def test_two_pages_parsed(self):
        """Page 1 (2 posts + after cursor) + page 2 (1 post + after=null) → 3 results."""
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=10, sort="new")

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_stops_at_after_null(self):
        """When page 2 has after=null the loop must stop even if limit not yet reached."""
        from scrapeforge.scrapers.community.reddit import RedditScraper

        # After page 2 (after=null) the scraper must NOT make a third request.
        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=100, sort="new")

        # Only 3 posts in total (2 on page 1 + 1 on page 2).
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_limit_respected(self):
        """Requesting limit=2 returns at most 2 articles even if more are available."""
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=2, sort="new")

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_all_results_are_success(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=10, sort="new")

        assert all(r.status == "success" for r in results)

    @pytest.mark.asyncio
    async def test_articles_are_attached(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=10, sort="new")

        assert all(r.article is not None for r in results)

    @pytest.mark.asyncio
    async def test_non_t3_children_skipped(self):
        """The t1 comment child in page 1 must be skipped (only t3 posts collected)."""
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=10, sort="new")

        # 3 t3 posts total across both pages; 1 t1 child should be ignored.
        assert len(results) == 3
        titles = {r.article.title for r in results if r.article}
        assert "First Python Post" in titles
        assert "Cool Python Tool" in titles
        assert "Third Python Post" in titles

    @pytest.mark.asyncio
    async def test_driver_used_is_curl_cffi(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=10, sort="new")

        assert all(r.driver_used == "curl_cffi" for r in results)

    @pytest.mark.asyncio
    async def test_url_built_correctly(self):
        """The first navigate call should use the right URL with limit and sort."""
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        await scraper.scrape_subreddit("python", limit=10, sort="new")

        first_call_url: str = fake_bridge.navigate.call_args_list[0][0][0]
        assert "reddit.com/r/python/new.json" in first_call_url
        assert "limit=" in first_call_url

    @pytest.mark.asyncio
    async def test_after_cursor_used_on_second_page(self):
        """The second navigate call must include the 'after' cursor from page 1."""
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        await scraper.scrape_subreddit("python", limit=10, sort="new")

        second_call_url: str = fake_bridge.navigate.call_args_list[1][0][0]
        assert "after=t3_def222" in second_call_url

    @pytest.mark.asyncio
    async def test_second_page_url_includes_count(self):
        """The second navigate call must include the running count from page 1.

        Page 1 had 3 raw children (2 t3 + 1 t1), so count passed to page 2 is 3.
        """
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        await scraper.scrape_subreddit("python", limit=10, sort="new")

        second_call_url: str = fake_bridge.navigate.call_args_list[1][0][0]
        # Page 1 returned 3 children (2 t3 posts + 1 t1 comment).
        assert "count=3" in second_call_url

    @pytest.mark.asyncio
    async def test_empty_first_page_returns_empty_list_and_logs_warning(self, caplog):
        """An empty first-page response (dist==0 / no children) returns [] without error.

        Per playbook §4.4: HTTP-200 with an empty first page on a known-active sub
        may signal a soft-block.  The scraper must log a WARNING and return gracefully.
        """
        import logging

        from scrapeforge.scrapers.community.reddit import RedditScraper

        empty_page_json = json.dumps(
            {
                "kind": "Listing",
                "data": {
                    "dist": 0,
                    "after": None,
                    "before": None,
                    "children": [],
                },
            }
        )

        fake_bridge = _make_fake_bridge(empty_page_json)
        scraper = RedditScraper(bridge=fake_bridge)

        with caplog.at_level(logging.WARNING, logger="scrapeforge.scrapers.community.reddit"):
            results = await scraper.scrape_subreddit("python", limit=10, sort="new")

        assert results == [], "empty first page must return an empty list"
        assert any(
            "soft-block" in rec.message.lower() or "empty" in rec.message.lower()
            for rec in caplog.records
        ), "a WARNING about possible soft-block or empty sub must be logged"

    @pytest.mark.asyncio
    async def test_returns_list_of_scrape_results(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_PAGE_1_JSON, _PAGE_2_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        results = await scraper.scrape_subreddit("python", limit=10, sort="new")

        assert isinstance(results, list)
        assert all(isinstance(r, ScrapeResult) for r in results)


# ---------------------------------------------------------------------------
# scrape() — single post URL
# ---------------------------------------------------------------------------


class TestScrapeMethod:
    @pytest.mark.asyncio
    async def test_returns_success(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_SINGLE_POST_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        result = await scraper.scrape(
            "https://www.reddit.com/r/Python/comments/xyz999/a_single_post/"
        )

        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_article_title_extracted(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_SINGLE_POST_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        result = await scraper.scrape(
            "https://www.reddit.com/r/Python/comments/xyz999/a_single_post/"
        )

        assert result.article is not None
        assert result.article.title == "A Single Post"

    @pytest.mark.asyncio
    async def test_article_author_extracted(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_SINGLE_POST_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        result = await scraper.scrape(
            "https://www.reddit.com/r/Python/comments/xyz999/a_single_post/"
        )

        assert result.article is not None
        assert result.article.author == "user_dave"

    @pytest.mark.asyncio
    async def test_appends_dot_json_if_missing(self):
        """scrape() should append .json to the URL when it's not already present."""
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_SINGLE_POST_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        url = "https://www.reddit.com/r/Python/comments/xyz999/a_single_post/"
        await scraper.scrape(url)

        navigated_url: str = fake_bridge.navigate.call_args_list[0][0][0]
        assert navigated_url.endswith(".json")

    @pytest.mark.asyncio
    async def test_returns_scrape_result_instance(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_SINGLE_POST_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        result = await scraper.scrape(
            "https://www.reddit.com/r/Python/comments/xyz999/a_single_post/"
        )

        assert isinstance(result, ScrapeResult)

    @pytest.mark.asyncio
    async def test_driver_used_curl_cffi(self):
        from scrapeforge.scrapers.community.reddit import RedditScraper

        fake_bridge = _make_fake_bridge(_SINGLE_POST_JSON)
        scraper = RedditScraper(bridge=fake_bridge)

        result = await scraper.scrape(
            "https://www.reddit.com/r/Python/comments/xyz999/a_single_post/"
        )

        assert result.driver_used == "curl_cffi"
