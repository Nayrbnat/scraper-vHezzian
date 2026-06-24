"""Tests for the Gemini embedder adapter (respx-mocked; no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scrapeforge.core.embeddings.exceptions import EmbeddingError, EmbeddingRateLimitError
from scrapeforge.core.embeddings.settings import EmbedderSettings

_BASE = "https://generativelanguage.googleapis.com/v1beta"
_MODEL = "gemini-embedding-001"
_URL = f"{_BASE}/models/{_MODEL}:batchEmbedContents"


def _settings(**over) -> EmbedderSettings:
    base = {"EMBED_API_KEY": "secret-key", "EMBED_DIM": 3, "EMBED_MAX_RETRIES": 1}
    base.update(over)
    return EmbedderSettings(**base)


def _resp(vectors: list[list[float]]) -> dict:
    return {"embeddings": [{"values": v} for v in vectors]}


@respx.mock
async def test_embeds_in_order() -> None:
    from scrapeforge.core.embeddings.gemini import GeminiEmbedder

    respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_resp([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]))
    )
    out = await GeminiEmbedder(_settings()).embed(["a", "b"])
    assert out == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


async def test_empty_input_returns_empty() -> None:
    from scrapeforge.core.embeddings.gemini import GeminiEmbedder

    assert await GeminiEmbedder(_settings()).embed([]) == []


@respx.mock
async def test_batches_by_batch_size() -> None:
    from scrapeforge.core.embeddings.gemini import GeminiEmbedder

    route = respx.post(_URL).mock(return_value=httpx.Response(200, json=_resp([[1.0, 0.0, 0.0]])))
    await GeminiEmbedder(_settings(EMBED_BATCH_SIZE=1)).embed(["a", "b", "c"])
    assert route.call_count == 3  # one request per item at batch size 1


@respx.mock
async def test_429_raises_rate_limit_after_retries(monkeypatch) -> None:
    import asyncio

    from scrapeforge.core.embeddings.gemini import GeminiEmbedder

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    route = respx.post(_URL).mock(return_value=httpx.Response(429, json={"error": "rate"}))
    with pytest.raises(EmbeddingRateLimitError):
        await GeminiEmbedder(_settings()).embed(["a"])
    assert route.call_count == 2  # initial + 1 retry


@respx.mock
async def test_timeout_raises_embedding_error_not_rate_limit(monkeypatch) -> None:
    import asyncio

    from scrapeforge.core.embeddings.gemini import GeminiEmbedder

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    respx.post(_URL).mock(side_effect=httpx.TimeoutException("slow"))
    with pytest.raises(EmbeddingError) as exc:
        await GeminiEmbedder(_settings()).embed(["a"])
    assert not isinstance(exc.value, EmbeddingRateLimitError)


@respx.mock
async def test_api_key_never_logged(caplog) -> None:
    import logging

    from scrapeforge.core.embeddings.gemini import GeminiEmbedder

    respx.post(_URL).mock(return_value=httpx.Response(200, json=_resp([[1.0, 0.0, 0.0]])))
    with caplog.at_level(logging.DEBUG):
        await GeminiEmbedder(_settings()).embed(["a"])
    assert "secret-key" not in caplog.text


@respx.mock
async def test_count_mismatch_raises_parse_error() -> None:
    from scrapeforge.core.embeddings.exceptions import EmbeddingParseError
    from scrapeforge.core.embeddings.gemini import GeminiEmbedder

    respx.post(_URL).mock(return_value=httpx.Response(200, json=_resp([[1.0, 0.0, 0.0]])))
    with pytest.raises(EmbeddingParseError):
        await GeminiEmbedder(_settings()).embed(["a", "b"])  # asked 2, got 1
