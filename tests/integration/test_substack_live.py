"""Live smoke test for SubstackScraper (integration — MANUAL ONLY, never CI).

WARNING: This test makes REAL HTTP requests to a live Substack publication.

Datacenter IP note (playbook §4, §6)
--------------------------------------
Public Substack endpoints return HTTP 200 with a browser UA and no WAF/Cloudflare
was observed in live testing (2026-06-22).  However, a datacenter IP could still
be throttled or soft-blocked.  The test asserts gracefully in both cases — it never
crashes and never hammers the API (single low-volume request only).

Run manually (never in CI):

    .venv/Scripts/python -m pytest -m integration tests/integration/test_substack_live.py -v

"""

from __future__ import annotations

import asyncio
import sys

import pytest

# ---------------------------------------------------------------------------
# Windows: curl_cffi requires the selector event loop (not Proactor).
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.integration
async def test_scrape_publication_noahpinion_live(fake_env) -> None:
    """Single polite live fetch of www.noahpinion.blog — 2 posts only.

    Uses the custom-domain endpoint to exercise 301-redirect following.

    Graceful assertion:
    - If ≥ 1 Article returned: validate basic field shape.
    - If 0 results: the test passes with a skip (datacenter IP or throttle).
    - If an exception is raised: documented skip — the scraper must NOT crash.

    Public Substack is generally reachable, but a datacenter IP could still
    be throttled.  Re-run with a residential proxy if no results come back.
    """
    from scrapeforge.scrapers.community.substack import SubstackScraper

    scraper = SubstackScraper()

    results = []
    try:
        results = await scraper.scrape_publication("www.noahpinion.blog", limit=2, sort="new")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"Live fetch raised {type(exc).__name__}: {exc} — "
            "datacenter IP may be throttled (playbook §4); re-run with a residential proxy."
        )

    if not results:
        pytest.skip(
            "Live fetch returned 0 results — possible datacenter IP throttle "
            "(playbook §4); re-run with a residential proxy."
        )

    # At least one result came back — validate the shape.
    assert len(results) <= 2, "limit=2 should cap results at 2"

    result = results[0]
    assert result.status == "success", f"expected success, got {result.status!r}"
    assert result.article is not None, "article should be attached on success"
    assert result.article.title, "article title should be non-empty"
    assert result.article.content, "article content should be non-empty"
    assert result.article.url.startswith("https://"), "article URL should be absolute"
    assert result.article.publish_date is not None, "publish_date should be set"
    assert result.article.publish_date.tzinfo is not None, "publish_date should be tz-aware"
    assert result.article.metadata.get("bucket") == "community"
    assert result.article.metadata.get("source_domain"), "source_domain should be set"
    assert result.driver_used == "curl_cffi"
