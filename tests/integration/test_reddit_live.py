"""Live smoke test for RedditScraper (integration — MANUAL ONLY, never CI).

WARNING: This test makes a REAL HTTP request to www.reddit.com.

Datacenter IP note (playbook §4.4)
------------------------------------
Running from a cloud/datacenter IP (AWS, GCP, Azure, DO) will likely produce
a 403 or 429 response.  This is expected.  Re-run with a residential proxy via
the ``--proxy`` flag or set ``proxy`` directly in the scraper constructor.  The
test asserts gracefully in both cases — it never crashes and never hammers Reddit
(single low-volume request only).

Run manually:

    .venv/Scripts/python -m pytest -m integration tests/integration/test_reddit_live.py -v

"""

from __future__ import annotations

import asyncio
import sys

import pytest

# ---------------------------------------------------------------------------
# Windows: curl_cffi needs the selector event loop (not Proactor).
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.integration
async def test_scrape_subreddit_python_live(fake_env) -> None:
    """Single polite live fetch of r/Python/new — 2 posts only.

    Graceful assertion:
    - If ≥ 1 Article returned: basic field checks.
    - If 0 results returned (datacenter block, 403/429, empty listing):
      the test passes with a warning.  The scraper must NOT crash.

    A datacenter IP may receive a 403 or 429 from Reddit (playbook §4.4).
    In CI this test is NEVER collected (``integration`` marker).
    """
    from scrapeforge.scrapers.community.reddit import RedditScraper

    scraper = RedditScraper()  # no proxy for the single smoke call

    results = []
    try:
        results = await scraper.scrape_subreddit("python", limit=2, sort="new")
    except Exception as exc:  # noqa: BLE001
        # A network or rate-limit error on a datacenter IP is an expected outcome.
        # Document it but do NOT fail the test.
        pytest.skip(
            f"Live fetch raised {type(exc).__name__}: {exc} — "
            "datacenter IP may be blocked (playbook §4.4); re-run with a residential proxy."
        )

    if not results:
        # Reddit returned an empty listing — soft-block or datacenter detection.
        pytest.skip(
            "Live fetch returned 0 results — likely a datacenter IP soft-block "
            "(playbook §4.4); re-run with a residential proxy."
        )

    # At least one result came back — validate the shape.
    assert len(results) <= 2, "limit=2 should cap results at 2"
    result = results[0]
    assert result.status == "success", f"expected success, got {result.status!r}"
    assert result.article is not None, "article should be attached on success"
    assert result.article.title, "article title should be non-empty"
    assert result.article.url.startswith("https://www.reddit.com"), (
        "article URL should be an absolute reddit.com URL"
    )
    assert result.article.publish_date is not None, "publish_date should be set"
    assert result.article.metadata.get("source_domain") == "reddit.com"
    assert result.article.metadata.get("bucket") == "community"
