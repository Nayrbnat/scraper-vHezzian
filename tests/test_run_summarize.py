"""Smoke tests: async main() is a coroutine; empty SUMMARY_API_KEY idles, never touches LLM."""

from __future__ import annotations

import asyncio
import inspect

import pytest


def test_entry_exposes_async_main() -> None:
    from scrapeforge.worker import run_summarize

    assert inspect.iscoroutinefunction(run_summarize.main)


async def test_empty_api_key_idles_never_constructs_llm(fake_env, monkeypatch) -> None:
    """With no SUMMARY_API_KEY, main() must idle (TimeoutError) and never touch the LLM client."""
    from scrapeforge.worker import run_summarize

    monkeypatch.setenv("SUMMARY_API_KEY", "")

    # Patch DB-touching callables so no real I/O occurs.
    async def _noop_ensure(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(run_summarize, "ensure_summary_columns", _noop_ensure)
    monkeypatch.setattr(run_summarize, "make_engine", lambda *_a, **_k: object())
    monkeypatch.setattr(run_summarize, "make_sessionmaker", lambda *_a, **_k: object())

    # Spy: record whether OpenAICompatibleSummarizer was ever constructed.
    llm_constructed = []

    class _SpySummarizer:
        def __init__(self, *_a, **_k) -> None:
            llm_constructed.append(True)

    monkeypatch.setattr(run_summarize, "OpenAICompatibleSummarizer", _SpySummarizer)

    # Spy: record whether run_summarize_worker was ever called.
    worker_called = []

    async def _spy_worker(*_a, **_k) -> None:
        worker_called.append(True)

    monkeypatch.setattr(run_summarize, "run_summarize_worker", _spy_worker)

    # Speed up the idle sleep so the timeout fires quickly.
    monkeypatch.setattr(run_summarize, "_POLL_INTERVAL_S", 0.01)

    # main() must idle forever (TimeoutError) — it must NOT return.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(run_summarize.main(), timeout=0.2)

    assert not llm_constructed, "OpenAICompatibleSummarizer must never be constructed without a key"
    assert not worker_called, "run_summarize_worker must never be called without a key"
