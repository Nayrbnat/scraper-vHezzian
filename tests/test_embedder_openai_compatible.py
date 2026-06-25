"""Tests for the OpenAI-compatible embedder (Jina) adapter (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scrapeforge.core.embeddings.exceptions import EmbeddingError, EmbeddingRateLimitError
from scrapeforge.core.embeddings.settings import EmbedderSettings

_BASE = "https://api.jina.ai/v1"
_URL = f"{_BASE}/embeddings"


def _settings(**over) -> EmbedderSettings:
    base = {
        "EMBED_PROVIDER": "openai_compatible",
        "EMBED_API_BASE_URL": _BASE,
        "EMBED_MODEL": "jina-embeddings-v4",
        "EMBED_API_KEY": "secret-key",
        "EMBED_DIM": 3,
        "EMBED_MAX_RETRIES": 1,
    }
    base.update(over)
    return EmbedderSettings(**base)


def _resp(vectors: list[list[float]]) -> dict:
    return {"data": [{"embedding": v} for v in vectors]}


@respx.mock
async def test_embeds_in_order() -> None:
    from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder

    respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_resp([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]))
    )
    out = await OpenAICompatibleEmbedder(_settings()).embed(["a", "b"])
    assert out == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


@respx.mock
async def test_429_raises_rate_limit(monkeypatch) -> None:
    import asyncio

    from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    respx.post(_URL).mock(return_value=httpx.Response(429, json={"error": "rate"}))
    with pytest.raises(EmbeddingRateLimitError):
        await OpenAICompatibleEmbedder(_settings()).embed(["a"])


@respx.mock
async def test_http_error_raises_embedding_error() -> None:
    from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder

    respx.post(_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(EmbeddingError):
        await OpenAICompatibleEmbedder(_settings()).embed(["a"])


@respx.mock
async def test_api_key_never_logged(caplog) -> None:
    import logging

    from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder

    respx.post(_URL).mock(return_value=httpx.Response(200, json=_resp([[1.0, 0.0, 0.0]])))
    with caplog.at_level(logging.DEBUG):
        await OpenAICompatibleEmbedder(_settings()).embed(["a"])
    assert "secret-key" not in caplog.text
