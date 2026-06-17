"""Phase 1.5 — Vertical Slice (integration, sandbox-only).

Proves the whole ingestion spine end-to-end on ONE real HTTP fetch:
  ScrapeEngine -> registry route (PublicScraper fallback) -> RateLimiter ->
  ProxyRotator (none) -> StealthBridge[curl_cffi] -> real TLS/JA4 GET ->
  validators.response_is_valid -> parsers.extract -> Article -> JsonlSink
  (JSONL line + manifest) -> resume (a fresh sink on the same path skips it).

Target is a SCRAPING SANDBOX (books.toscrape.com) — purpose-built, zero anti-bot,
no Cloudflare/DNS-block risk. Real protected sites stay out of CI and out of this
environment. Marked ``integration`` so it NEVER runs in CI; run manually:

    .venv/Scripts/python -m pytest -m integration tests/integration/test_vertical_slice.py -v
"""

from __future__ import annotations

import pytest

from scrapeforge.core.engine import ScrapeEngine
from scrapeforge.core.storage.jsonl import JsonlSink

# A single public book page on the sandbox. <article class="product_page"> gives the
# generic PublicScraper content selector real text to extract (> the 500-char floor).
SANDBOX_URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"


@pytest.mark.integration
async def test_vertical_slice_end_to_end(tmp_path, fake_env) -> None:
    out = tmp_path / "slice"
    sink = JsonlSink(out)
    engine = ScrapeEngine(sink=sink)

    # --- One real URL through the full pipeline ---
    result = await engine.scrape(SANDBOX_URL)

    assert result.status == "success", f"expected success, got {result.status!r}: {result.error!r}"
    assert result.article is not None
    assert result.article.title.strip(), "extracted title should be non-empty"
    assert result.driver_used == "curl_cffi"

    # --- Persisted: JSONL line + manifest entry ---
    assert sink.path.exists(), "JSONL output file should exist"
    assert sink.manifest_path.exists(), "resume manifest should exist"
    assert sink.seen(SANDBOX_URL), "sink should report the URL as seen after write"

    # --- Resumable: a fresh sink on the same path sees the prior manifest ---
    resumed = JsonlSink(out)
    assert resumed.seen(SANDBOX_URL), "a new sink on the same path must skip the completed URL"
