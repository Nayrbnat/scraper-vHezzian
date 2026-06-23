"""Live smoke for the summarizer (integration — MANUAL ONLY, never CI).

Requires a real SUMMARY_API_KEY in the environment / .env. Skips gracefully otherwise.

Run:
    .venv/Scripts/python -m pytest -m integration tests/integration/test_summarize_live.py -v
"""

from __future__ import annotations

import pytest

from scrapeforge.core.llm.settings import SummarizerSettings


@pytest.mark.integration
async def test_live_glm_summary() -> None:
    settings = SummarizerSettings()
    if not settings.SUMMARY_API_KEY:
        pytest.skip("SUMMARY_API_KEY not set — set it to run the live summarizer smoke.")

    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    article = (
        "TSMC said its next-generation advanced packaging, including hybrid bonding, will ramp "
        "in 2027 to feed AI accelerator demand from Nvidia and others, with capex rising sharply."
    )
    try:
        out = await OpenAICompatibleSummarizer(settings).summarize(
            title="TSMC advanced packaging roadmap",
            content=article,
            published=None,
            portfolio=["Nvidia", "TSMC"],
            interests=["hybrid bonding", "advanced packaging"],
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live LLM call failed ({type(exc).__name__}: {exc}) — check key/endpoint.")

    assert 3 <= len(out.bullets) <= 5
    assert all(b.strip() for b in out.bullets)
    assert 1 <= out.relevance <= 10
    assert set(out.scores) == {"relevance", "credibility", "intensity", "personal", "time"}
    assert all(1 <= v <= 10 for v in out.scores.values())
