"""parse_news_feed: RSS 2.0 XML -> ScrapeResults (pure; no network)."""

from __future__ import annotations

from scrapeforge.scrapers.public.news_rss import parse_news_feed

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example News</title>
    <item>
      <title>Big Funding Round</title>
      <link>https://news.example.com/a</link>
      <pubDate>Wed, 24 Jun 2026 10:00:00 +0000</pubDate>
      <dc:creator>Jane Reporter</dc:creator>
      <description>Short blurb.</description>
      <content:encoded><![CDATA[<p>Full <b>article</b> body with plenty
        of words here to pass the floor.</p>]]></content:encoded>
    </item>
    <item>
      <title>Description Only</title>
      <link>https://news.example.com/b</link>
      <description><![CDATA[<p>This item has only a description, no
        content:encoded block at all.</p>]]></description>
    </item>
    <item>
      <title>No Link Skipped</title>
      <description>orphan</description>
    </item>
  </channel>
</rss>"""


def test_parses_items_with_content_or_description() -> None:
    results = parse_news_feed(_FEED, limit=10, min_chars=0)
    # 2 items have a link (the third is skipped — no link).
    assert len(results) == 2
    first = results[0].article
    assert first.url == "https://news.example.com/a"
    assert first.title == "Big Funding Round"
    assert "Full article body" in first.content  # content:encoded, HTML stripped to text
    assert first.author == "Jane Reporter"
    assert first.publish_date is not None
    assert first.metadata["bucket"] == "public"
    assert first.metadata["via"] == "rss"
    assert first.metadata["source_domain"] == "news.example.com"


def test_description_fallback_when_no_content_encoded() -> None:
    results = parse_news_feed(_FEED, limit=10, min_chars=0)
    second = results[1].article
    assert second.url == "https://news.example.com/b"
    assert "only a description" in second.content


def test_min_chars_skips_thin_items() -> None:
    # A high floor drops both (their cleaned text is short); resilient, never raises.
    results = parse_news_feed(_FEED, limit=10, min_chars=5000)
    assert results == []


def test_limit_caps_results() -> None:
    assert len(parse_news_feed(_FEED, limit=1, min_chars=0)) == 1


def test_malformed_xml_returns_empty() -> None:
    assert parse_news_feed("<not xml", limit=10) == []
