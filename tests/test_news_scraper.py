"""NewsScraper.scrape_feed fetches a feed via the bridge and parses it (fake bridge; no network)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from scrapeforge.scrapers.public.news_scraper import NewsScraper

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title>Hello</title>
      <link>https://news.example.com/a</link>
      <content:encoded><![CDATA[<p>Plenty of real body text right
        here in the feed.</p>]]></content:encoded>
    </item>
  </channel>
</rss>"""


def _fake_bridge(xml: str) -> MagicMock:
    bridge = MagicMock()
    bridge.driver = "curl_cffi"
    bridge.navigate = AsyncMock()
    bridge.get_html = AsyncMock(return_value=xml)
    bridge.__aenter__ = AsyncMock(return_value=bridge)
    bridge.__aexit__ = AsyncMock(return_value=False)
    return bridge


async def test_scrape_feed_fetches_and_parses() -> None:
    bridge = _fake_bridge(_FEED)
    scraper = NewsScraper(bridge=bridge)

    results = await scraper.scrape_feed("https://news.example.com/feed/", limit=10)

    assert len(results) == 1
    assert results[0].article.url == "https://news.example.com/a"
    assert "real body text" in results[0].article.content
    bridge.navigate.assert_awaited_once_with("https://news.example.com/feed/")
