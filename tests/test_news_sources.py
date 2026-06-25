"""Curated news-RSS feed list + selection helpers."""

from __future__ import annotations

from scrapeforge.scrapers.public.news_sources import (
    NEWS_RSS_FEEDS,
    NewsFeed,
    by_sector,
    sectors,
    select_feeds,
)


def test_feeds_nonempty_and_unique() -> None:
    assert len(NEWS_RSS_FEEDS) >= 5
    urls = [f.feed_url for f in NEWS_RSS_FEEDS]
    assert len(urls) == len(set(urls))  # no duplicate feed URLs


def test_includes_techcrunch_and_crunchbase() -> None:
    hosts = " ".join(f.feed_url for f in NEWS_RSS_FEEDS)
    assert "techcrunch.com" in hosts
    assert "crunchbase.com" in hosts


def test_feed_urls_are_absolute_https() -> None:
    for f in NEWS_RSS_FEEDS:
        assert f.feed_url.startswith("https://")


def test_select_by_sector_and_limit() -> None:
    tech = select_feeds(sector="Tech & Startups")
    assert tech and all(f.sector == "Tech & Startups" for f in tech)
    assert by_sector("Tech & Startups") == tech
    assert len(select_feeds(limit=2)) == 2
    assert select_feeds(limit=None) == NEWS_RSS_FEEDS


def test_sectors_distinct() -> None:
    labels = sectors()
    assert len(labels) == len(set(labels))


def test_dataclass_shape() -> None:
    f = NewsFeed(
        name="TechCrunch", feed_url="https://techcrunch.com/feed/", sector="Tech & Startups"
    )
    assert f.feed_url.endswith("/feed/")
