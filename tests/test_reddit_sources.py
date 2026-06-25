"""Curated Reddit subreddit source list + selection helpers."""

from __future__ import annotations

from scrapeforge.scrapers.community.reddit_sources import (
    REDDIT_INVESTING_SUBREDDITS,
    RedditSource,
    by_sector,
    sectors,
    select_subreddits,
)


def test_sources_nonempty_and_unique() -> None:
    assert len(REDDIT_INVESTING_SUBREDDITS) >= 12
    names = [s.subreddit for s in REDDIT_INVESTING_SUBREDDITS]
    assert len(names) == len(set(names))  # no duplicate subreddits


def test_subreddit_has_no_r_prefix() -> None:
    # Stored bare (no "r/") because scrape_subreddit adds the path itself.
    for s in REDDIT_INVESTING_SUBREDDITS:
        assert not s.subreddit.startswith("r/")
        assert "/" not in s.subreddit


def test_select_by_sector() -> None:
    investing = select_subreddits(sector="Investing")
    assert investing
    assert all(s.sector == "Investing" for s in investing)
    assert by_sector("Investing") == investing


def test_select_limit() -> None:
    assert len(select_subreddits(limit=3)) == 3
    assert select_subreddits(limit=None) == REDDIT_INVESTING_SUBREDDITS


def test_sectors_distinct_in_order() -> None:
    labels = sectors()
    assert len(labels) == len(set(labels))
    assert "Investing" in labels


def test_dataclass_shape() -> None:
    s = RedditSource(name="Investing", subreddit="investing", sector="Investing")
    assert s.subreddit == "investing"
