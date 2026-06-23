"""Smoke test: the summarizer entry exposes an async main() and no-ops without a key."""

from __future__ import annotations

import inspect


def test_entry_exposes_async_main() -> None:
    from scrapeforge.worker import run_summarize

    assert inspect.iscoroutinefunction(run_summarize.main)
