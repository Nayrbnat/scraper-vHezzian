"""Curated investing + AI/tech subreddit source list — Bucket 2 (community).

Mirrors :mod:`scrapeforge.scrapers.community.substack_sources`: a hand-picked catalogue plus
selection helpers. The :class:`~scrapeforge.scrapers.community.reddit.RedditScraper` already
self-registers for ``reddit.com``; this module just names the subreddits to point it at. Adding a
curated source list is extension by addition (CLAUDE.md §2, Invariant #16) — one new file, no edits
to a shared seam.

Each ``subreddit`` is stored **bare** (no ``r/`` prefix): ``RedditScraper.scrape_subreddit`` builds
the ``/r/{subreddit}/{sort}.json`` path itself.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RedditSource:
    """One curated subreddit.

    Attributes:
        name:      Human-readable label (unique within the list).
        subreddit: Bare subreddit name, no ``r/`` prefix (e.g. ``SecurityAnalysis``).
        sector:    Coarse bucket for the ``--sector`` filter and list balance.
    """

    name: str
    subreddit: str
    sector: str


# ---------------------------------------------------------------------------
# The curated set — investing/equities, trading/quant, macro, and the AI/tech lane.
# ---------------------------------------------------------------------------
REDDIT_INVESTING_SUBREDDITS: tuple[RedditSource, ...] = (
    # --- Investing & equity research ---------------------------------------
    RedditSource("r/investing", "investing", "Investing"),
    RedditSource("r/stocks", "stocks", "Investing"),
    RedditSource("r/SecurityAnalysis", "SecurityAnalysis", "Investing"),
    RedditSource("r/ValueInvesting", "ValueInvesting", "Investing"),
    RedditSource("r/StockMarket", "StockMarket", "Investing"),
    RedditSource("r/Bogleheads", "Bogleheads", "Investing"),
    RedditSource("r/dividends", "dividends", "Investing"),
    # --- Trading & quant ----------------------------------------------------
    RedditSource("r/options", "options", "Trading & Quant"),
    RedditSource("r/wallstreetbets", "wallstreetbets", "Trading & Quant"),
    RedditSource("r/algotrading", "algotrading", "Trading & Quant"),
    RedditSource("r/quant", "quant", "Trading & Quant"),
    # --- Macro & finance ----------------------------------------------------
    RedditSource("r/economics", "economics", "Macro & Finance"),
    RedditSource("r/finance", "finance", "Macro & Finance"),
    # --- AI & tech (matches SUMMARY_FOCUS) ----------------------------------
    RedditSource("r/artificial", "artificial", "AI & Tech"),
    RedditSource("r/MachineLearning", "MachineLearning", "AI & Tech"),
    RedditSource("r/LocalLLaMA", "LocalLLaMA", "AI & Tech"),
    RedditSource("r/hardware", "hardware", "AI & Tech"),
    RedditSource("r/technology", "technology", "AI & Tech"),
)


# ---------------------------------------------------------------------------
# Selection helpers — the ingest job / CLI use these to choose what to scrape.
# ---------------------------------------------------------------------------


def sectors() -> tuple[str, ...]:
    """Return the distinct sector labels in first-seen (list) order."""
    seen: dict[str, None] = {}
    for s in REDDIT_INVESTING_SUBREDDITS:
        seen.setdefault(s.sector, None)
    return tuple(seen)


def by_sector(sector: str) -> tuple[RedditSource, ...]:
    """Return the curated subreddits whose ``sector`` matches *sector* exactly."""
    return tuple(s for s in REDDIT_INVESTING_SUBREDDITS if s.sector == sector)


def select_subreddits(
    *,
    sector: str | None = None,
    limit: int | None = None,
) -> tuple[RedditSource, ...]:
    """Pick curated subreddits to scrape.

    Args:
        sector: If given, keep only subreddits in this sector (exact match).
        limit:  If given, cap the result to the first *limit* subreddits.

    Returns:
        The selected subreddits, in curated (sector-grouped) order.
    """
    items = REDDIT_INVESTING_SUBREDDITS if sector is None else by_sector(sector)
    return items if limit is None else tuple(items[:limit])
