"""Smoke test: the community-ingest deployment entry imports and exposes main()."""

from __future__ import annotations


def test_entry_point_exposes_async_main() -> None:
    import inspect

    from scrapeforge.worker import run_community_ingest

    assert inspect.iscoroutinefunction(run_community_ingest.main)
