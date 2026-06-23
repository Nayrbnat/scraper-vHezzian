"""Contract tests for the Summarizer port + SummaryResult + LLM exceptions."""

from __future__ import annotations

import pytest


def test_llm_exception_hierarchy() -> None:
    from scrapeforge.core.llm.exceptions import LLMError, LLMParseError, LLMRateLimitError
    from scrapeforge.exceptions import ScrapeForgeError

    assert issubclass(LLMError, ScrapeForgeError)
    assert issubclass(LLMRateLimitError, LLMError)
    assert issubclass(LLMParseError, LLMError)


def test_summary_result_is_frozen_with_fields() -> None:
    from scrapeforge.core.llm.base import SummaryResult

    r = SummaryResult(
        bullets=["a", "b", "c"],
        relevance=8,
        scores={"relevance": 9, "credibility": 7, "intensity": 6, "personal": 8, "time": 5},
        reason="why",
        model="glm-4.5-flash",
    )
    assert r.bullets == ["a", "b", "c"]
    assert r.relevance == 8
    assert r.scores["personal"] == 8
    with pytest.raises(Exception):  # frozen dataclass
        r.relevance = 1  # type: ignore[misc]


async def test_summarizer_is_abstract() -> None:
    from scrapeforge.core.llm.base import Summarizer

    with pytest.raises(TypeError):
        Summarizer()  # type: ignore[abstract]
