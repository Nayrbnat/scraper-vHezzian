"""Curated public-news RSS feed list — Bucket 3 (public).

Mirrors :mod:`scrapeforge.scrapers.community.substack_sources`: a hand-picked catalogue plus
selection helpers. These are **RSS feed URLs** (not single articles); the
:class:`~scrapeforge.scrapers.public.news_scraper.NewsScraper` fetches each feed and parses its
items. Adding a curated source list is extension by addition (CLAUDE.md §2, Invariant #16) — one new
file, no edits to a shared seam.

All feeds are RSS 2.0 (``<item>`` + ``content:encoded``/``description``). Atom-only feeds are out of
scope for v1's parser. The daily ingest isolates per-feed failures, so a feed that moves or dies is
skipped without aborting the rest — but the list is worth a periodic live re-verify.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NewsFeed:
    """One curated news RSS feed.

    Attributes:
        name:     Human-readable outlet name (unique within the list).
        feed_url: Absolute ``https://`` RSS feed URL.
        sector:   Coarse bucket for the ``--sector`` filter and list balance.
    """

    name: str
    feed_url: str
    sector: str


# ---------------------------------------------------------------------------
# The curated set — startups/VC + tech + markets, all RSS 2.0.
# ---------------------------------------------------------------------------
NEWS_RSS_FEEDS: tuple[NewsFeed, ...] = (
    # --- Startups, funding & VC --------------------------------------------
    NewsFeed("TechCrunch", "https://techcrunch.com/feed/", "Tech & Startups"),
    NewsFeed("Crunchbase News", "https://news.crunchbase.com/feed/", "Funding & VC"),
    # --- Tech & AI ----------------------------------------------------------
    NewsFeed("VentureBeat", "https://venturebeat.com/feed/", "Tech & Startups"),
    NewsFeed("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "Tech & Startups"),
    # --- Markets & finance --------------------------------------------------
    NewsFeed(
        "CNBC Top News",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "Markets & Finance",
    ),
    NewsFeed(
        "MarketWatch Top Stories",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "Markets & Finance",
    ),
)


# ---------------------------------------------------------------------------
# Selection helpers — the ingest job / CLI use these to choose what to scrape.
# ---------------------------------------------------------------------------


def sectors() -> tuple[str, ...]:
    """Return the distinct sector labels in first-seen (list) order."""
    seen: dict[str, None] = {}
    for f in NEWS_RSS_FEEDS:
        seen.setdefault(f.sector, None)
    return tuple(seen)


def by_sector(sector: str) -> tuple[NewsFeed, ...]:
    """Return the curated feeds whose ``sector`` matches *sector* exactly."""
    return tuple(f for f in NEWS_RSS_FEEDS if f.sector == sector)


def select_feeds(
    *,
    sector: str | None = None,
    limit: int | None = None,
) -> tuple[NewsFeed, ...]:
    """Pick curated feeds to scrape.

    Args:
        sector: If given, keep only feeds in this sector (exact match).
        limit:  If given, cap the result to the first *limit* feeds.

    Returns:
        The selected feeds, in curated (sector-grouped) order.
    """
    items = NEWS_RSS_FEEDS if sector is None else by_sector(sector)
    return items if limit is None else tuple(items[:limit])
