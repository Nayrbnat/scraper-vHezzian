"""Tests for the OpenAI-compatible summarizer adapter (respx-mocked; no network)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scrapeforge.core.llm.exceptions import LLMParseError, LLMRateLimitError
from scrapeforge.core.llm.settings import SummarizerSettings

_BASE = "https://api.z.ai/api/paas/v4"
_URL = f"{_BASE}/chat/completions"


def _settings(fake_env, **over) -> SummarizerSettings:
    base = {
        "SUMMARY_API_KEY": "secret-key",
        "SUMMARY_API_BASE_URL": _BASE,
        "SUMMARY_MAX_RETRIES": 1,
    }
    base.update(over)
    return SummarizerSettings(**base)


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _good_json() -> str:
    return json.dumps(
        {
            "bullets": ["b1", "b2", "b3", "b4", "b5"],
            "scores": {"relevance": 9, "credibility": 8, "intensity": 7, "personal": 10, "time": 4},
            "relevance": 8,
            "reason": "tracked niche; fresh",
        }
    )


@respx.mock
async def test_parses_full_object(fake_env) -> None:
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    respx.post(_URL).mock(return_value=httpx.Response(200, json=_completion(_good_json())))
    out = await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
        title="T", content="C", published=None, portfolio=["Nvidia"], interests=["hybrid bonding"]
    )
    assert out.bullets == ["b1", "b2", "b3", "b4", "b5"]
    assert out.relevance == 8
    assert out.scores == {
        "relevance": 9,
        "credibility": 8,
        "intensity": 7,
        "personal": 10,
        "time": 4,
    }
    assert out.reason == "tracked niche; fresh"
    assert out.model == "glm-4.5-flash"


@respx.mock
async def test_clamps_out_of_range_scores(fake_env) -> None:
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    body = json.dumps(
        {
            "bullets": ["b1", "b2", "b3"],
            "scores": {"relevance": 99, "credibility": 0, "intensity": 5, "personal": 5, "time": 5},
            "relevance": 50,
            "reason": "x",
        }
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, json=_completion(body)))
    out = await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
        title="T", content="C", published=None, portfolio=[], interests=[]
    )
    assert out.relevance == 10
    assert out.scores["relevance"] == 10 and out.scores["credibility"] == 1


@respx.mock
async def test_fewer_than_three_bullets_raises(fake_env) -> None:
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    body = json.dumps({"bullets": ["only one"], "scores": {}, "relevance": 5, "reason": "x"})
    respx.post(_URL).mock(return_value=httpx.Response(200, json=_completion(body)))
    with pytest.raises(LLMParseError):
        await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
            title="T", content="C", published=None, portfolio=[], interests=[]
        )


@respx.mock
async def test_malformed_json_raises_parse_error(fake_env) -> None:
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    respx.post(_URL).mock(return_value=httpx.Response(200, json=_completion("not json at all")))
    with pytest.raises(LLMParseError):
        await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
            title="T", content="C", published=None, portfolio=[], interests=[]
        )


@respx.mock
async def test_429_retries_then_rate_limit_error(fake_env, monkeypatch) -> None:
    import asyncio

    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    route = respx.post(_URL).mock(return_value=httpx.Response(429, json={"error": "rate"}))
    with pytest.raises(LLMRateLimitError):
        await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
            title="T", content="C", published=None, portfolio=[], interests=[]
        )
    assert route.call_count == 2  # initial + 1 retry (SUMMARY_MAX_RETRIES=1)


@respx.mock
async def test_api_key_never_logged(fake_env, caplog) -> None:
    import logging

    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    respx.post(_URL).mock(return_value=httpx.Response(200, json=_completion(_good_json())))
    with caplog.at_level(logging.DEBUG):
        await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
            title="T", content="C", published=None, portfolio=[], interests=[]
        )
    assert "secret-key" not in caplog.text


@respx.mock
async def test_empty_choices_raises_parse_error(fake_env) -> None:
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    respx.post(_URL).mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMParseError, match="malformed completion envelope"):
        await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
            title="T", content="C", published=None, portfolio=[], interests=[]
        )


@respx.mock
async def test_bool_score_raises_parse_error(fake_env) -> None:
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer

    body = json.dumps(
        {
            "bullets": ["b1", "b2", "b3"],
            "scores": {
                "relevance": True,
                "credibility": 5,
                "intensity": 5,
                "personal": 5,
                "time": 5,
            },
            "relevance": 5,
            "reason": "x",
        }
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, json=_completion(body)))
    with pytest.raises(LLMParseError):
        await OpenAICompatibleSummarizer(_settings(fake_env)).summarize(
            title="T", content="C", published=None, portfolio=[], interests=[]
        )
