"""Parse a public-news RSS 2.0 feed into ScrapeResults (Bucket 3 — public).

Generic counterpart to :mod:`scrapeforge.scrapers.community.substack_rss`: same RSS-2.0 +
``content:encoded`` shape that TechCrunch, Crunchbase News, VentureBeat, Ars Technica, CNBC and most
WordPress-style outlets emit. Differences from the Substack parser: it tags ``bucket="public"`` and
falls back to ``<description>`` when an item has no ``content:encoded`` body (common for outlets
that publish only a summary to RSS). Pure function — no network, no driver — unit-tested against a
fixed XML fixture. Malformed XML returns an empty list (resilient).
"""

from __future__ import annotations

import logging
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from lxml import etree
from selectolax.parser import HTMLParser

from scrapeforge.core.models import Article, ScrapeResult

log = logging.getLogger(__name__)

_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
_DC_NS = "http://purl.org/dc/elements/1.1/"


def _text(el: etree._Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _html_to_text(html: str) -> str:
    """Strip HTML to collapsed plain text (selectolax — the project's sanctioned parser)."""
    if not html:
        return ""
    text = HTMLParser(html).text(separator=" ")
    return " ".join(text.split())


def _body_html(item: etree._Element) -> str:
    """Prefer ``content:encoded``; fall back to ``<description>`` (summary-only feeds)."""
    encoded = item.find(f"{{{_CONTENT_NS}}}encoded")
    if encoded is not None and encoded.text:
        return encoded.text
    description = item.find("description")
    return description.text if (description is not None and description.text) else ""


def parse_news_feed(xml: str, *, limit: int, min_chars: int = 0) -> list[ScrapeResult]:
    """Parse news feed *xml* into up to *limit* successful ScrapeResults.

    Items without a ``<link>`` or whose cleaned body text is shorter than *min_chars* are skipped.
    Malformed XML returns an empty list (resilient, like the Substack/JSON parsers).
    """
    try:
        root = etree.fromstring(xml.encode("utf-8"))
    except (etree.XMLSyntaxError, ValueError) as exc:
        log.warning("news_rss: unparseable feed XML: %s", exc)
        return []

    results: list[ScrapeResult] = []
    skipped = 0
    for item in root.iterfind(".//item"):
        if len(results) >= limit:
            break
        url = _text(item.find("link"))
        if not url:
            continue

        raw_html = _body_html(item)
        content = _html_to_text(raw_html)
        if len(content) < min_chars:
            skipped += 1
            continue

        published = None
        raw_date = _text(item.find("pubDate"))
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                log.debug("news_rss: unparseable pubDate %r", raw_date)

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
                "bucket": "public",
                "via": "rss",
            },
        )
        results.append(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    if skipped:
        log.info("news_rss: skipped %d thin/empty item(s)", skipped)
    return results
