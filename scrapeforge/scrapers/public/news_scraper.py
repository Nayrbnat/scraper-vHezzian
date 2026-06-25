"""NewsScraper — fetch a public-news RSS feed and parse it (Bucket 3 — public).

Subclasses :class:`~scrapeforge.scrapers.public.public.PublicScraper` to reuse its bridge lifecycle
(curl_cffi, fresh bridge per call unless one is injected — Invariant #7). The inherited
single-article ``scrape(url)`` is unchanged; this adds the **feed** path the daily ``ingest-news``
job drives. NOT ``@register_scraper``-decorated — it is invoked directly by feed URL, not by domain
routing.
"""

from __future__ import annotations

from scrapeforge.core.models import ScrapeResult
from scrapeforge.scrapers.public.news_rss import parse_news_feed
from scrapeforge.scrapers.public.public import PublicScraper


class NewsScraper(PublicScraper):
    """Fetch an RSS feed URL and parse its items into ScrapeResults."""

    async def scrape_feed(
        self, feed_url: str, *, limit: int = 25, min_chars: int = 0
    ) -> list[ScrapeResult]:
        """Fetch *feed_url* via the bridge and parse up to *limit* items (body >= *min_chars*).

        One request per feed (it lists ~20 recent items with their bodies inline), so it never
        hammers a site. Mirrors ``SubstackScraper.scrape_publication_via_rss``.
        """
        bridge = self.bridge if self.bridge is not None else self._create_default_bridge(self.proxy)
        async with bridge as b:
            await b.navigate(feed_url)
            xml = await b.get_html()
        return parse_news_feed(xml, limit=limit, min_chars=min_chars)
