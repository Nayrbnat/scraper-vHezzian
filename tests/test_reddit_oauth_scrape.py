"""RedditScraper.scrape_subreddit routes through oauth.reddit.com with a Bearer token (respx)."""

from __future__ import annotations

import httpx
import respx

from scrapeforge.scrapers.community.reddit import RedditScraper

_LISTING = {
    "kind": "Listing",
    "data": {
        "after": None,
        "dist": 1,
        "children": [
            {
                "kind": "t3",
                "data": {
                    "id": "x1",
                    "title": "DD on NVDA",
                    "selftext": "real analysis body",
                    "permalink": "/r/investing/comments/x1/dd/",
                    "url": "https://www.reddit.com/r/investing/comments/x1/dd/",
                    "author": "u1",
                    "created_utc": 1_700_000_000.0,
                    "subreddit": "investing",
                    "score": 120,
                    "num_comments": 5,
                    "is_self": True,
                },
            }
        ],
    },
}


class _FakeAuth:
    """Stands in for RedditAuth — returns a fixed Bearer token without a token POST."""

    async def token(self) -> str:
        return "tok-abc"


@respx.mock
async def test_scrape_subreddit_uses_oauth_endpoint() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json=_LISTING)

    respx.get(host="oauth.reddit.com").mock(side_effect=_handler)

    scraper = RedditScraper(auth=_FakeAuth())
    results = await scraper.scrape_subreddit("investing", limit=5, sort="hot")

    assert len(results) == 1
    assert results[0].article.title == "DD on NVDA"
    assert results[0].article.content == "real analysis body"
    # Hit the authenticated endpoint with the Bearer token + a descriptive UA.
    assert "oauth.reddit.com/r/investing/hot?" in captured["url"]
    # oauth.reddit.com serves JSON natively — the www-only `.json` suffix must be stripped.
    assert ".json" not in captured["url"]
    assert captured["auth"] == "Bearer tok-abc"
    assert captured["ua"]


@respx.mock
async def test_oauth_non_200_raises_driver_error() -> None:
    import pytest

    from scrapeforge.exceptions import DriverError

    respx.get(host="oauth.reddit.com").mock(return_value=httpx.Response(403, text="blocked"))
    scraper = RedditScraper(auth=_FakeAuth())
    with pytest.raises(DriverError):
        await scraper.scrape_subreddit("investing", limit=5, sort="hot")
