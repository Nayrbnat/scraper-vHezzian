"""Parse a Substack RSS feed (``<base>/feed``) into ScrapeResults.

Why RSS: the ``/api/v1/archive`` and ``/api/v1/posts/<slug>`` JSON endpoints get HTTP 429 /
Cloudflare-challenged from datacenter IPs (e.g. GitHub Actions runners). The RSS ``/feed``
endpoint is served differently and is commonly reachable where the JSON API is not. It carries
the post HTML in ``content:encoded`` for publications that publish full text to RSS (~60% of the
curated list); feeds that truncate their body yield too little text and are skipped (``min_chars``).

Pure function — no network, no driver — so it unit-tests against a fixed XML fixture. The scraper
(:meth:`SubstackScraper.scrape_publication_via_rss`) fetches the feed and hands the XML here.
"""

from __future__ import annotations

import logging
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from lxml import etree

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.scrapers.community.substack import _clean_html

log = logging.getLogger(__name__)

_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
_DC_NS = "http://purl.org/dc/elements/1.1/"


def _text(el: etree._Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_substack_feed(xml: str, *, limit: int, min_chars: int = 0) -> list[ScrapeResult]:
    """Parse Substack feed *xml* into up to *limit* successful ScrapeResults.

    Items whose cleaned ``content:encoded`` text is shorter than *min_chars* (truncated feeds) or
    that lack a link are skipped. Malformed XML returns an empty list (resilient, like JSON).
    """
    try:
        root = etree.fromstring(xml.encode("utf-8"))
    except (etree.XMLSyntaxError, ValueError) as exc:
        log.warning("substack_rss: unparseable feed XML: %s", exc)
        return []

    results: list[ScrapeResult] = []
    skipped = 0
    for item in root.iterfind(".//item"):
        if len(results) >= limit:
            break
        url = _text(item.find("link"))
        if not url:
            continue

        encoded = item.find(f"{{{_CONTENT_NS}}}encoded")
        raw_html = encoded.text if (encoded is not None and encoded.text) else ""
        content = _clean_html(raw_html) if raw_html else ""
        if len(content) < min_chars:
            skipped += 1
            continue

        published = None
        raw_date = _text(item.find("pubDate"))
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                log.debug("substack_rss: unparseable pubDate %r", raw_date)

        creator = item.find(f"{{{_DC_NS}}}creator")
        article = Article(
            url=url,
            title=_text(item.find("title")),
            content=content,
            author=_text(creator) or None,
            publish_date=published,
            raw_html=raw_html or None,
            metadata={
                "source_domain": urlsplit(url).hostname or "",
                "bucket": "community",
                "via": "rss",
            },
        )
        results.append(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    if skipped:
        log.info("substack_rss: skipped %d truncated/thin item(s)", skipped)
    return results
