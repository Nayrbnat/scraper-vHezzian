"""parse_substack_feed turns a Substack RSS feed into ScrapeResults (full-text items only).

RSS (`<base>/feed`) is a different endpoint from the rate-limited `/api/v1` JSON API. It carries
post HTML in `content:encoded` for publications that publish full text; truncated feeds yield too
little text and are skipped via the `min_chars` gate.
"""

from __future__ import annotations

from scrapeforge.scrapers.community.substack_rss import parse_substack_feed

_FULL_BODY = "<p>" + ("Nvidia margins, TSMC capacity, advanced packaging demand. " * 40) + "</p>"

_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Test Pub</title>
    <item>
      <title><![CDATA[Full Post]]></title>
      <link>https://pub.substack.com/p/full-post</link>
      <dc:creator><![CDATA[Jane Doe]]></dc:creator>
      <pubDate>Tue, 19 May 2026 14:34:11 GMT</pubDate>
      <content:encoded><![CDATA[{_FULL_BODY}]]></content:encoded>
    </item>
    <item>
      <title><![CDATA[Truncated Post]]></title>
      <link>https://pub.substack.com/p/truncated</link>
      <content:encoded><![CDATA[<p><a href="x">Read more</a></p>]]></content:encoded>
    </item>
  </channel>
</rss>"""


def test_parses_full_content_item() -> None:
    results = parse_substack_feed(_FEED, limit=10, min_chars=500)
    assert len(results) == 1  # truncated item skipped
    art = results[0].article
    assert results[0].status == "success"
    assert art.url == "https://pub.substack.com/p/full-post"
    assert art.title == "Full Post"
    assert art.author == "Jane Doe"
    assert art.publish_date is not None and art.publish_date.year == 2026
    assert "Nvidia" in art.content
    assert art.metadata["via"] == "rss"


def test_skips_truncated_item_below_min_chars() -> None:
    urls = [r.article.url for r in parse_substack_feed(_FEED, limit=10, min_chars=500)]
    assert "https://pub.substack.com/p/truncated" not in urls


def test_respects_limit() -> None:
    assert len(parse_substack_feed(_FEED, limit=1, min_chars=0)) == 1


def test_malformed_xml_returns_empty() -> None:
    assert parse_substack_feed("<not valid xml", limit=10) == []
