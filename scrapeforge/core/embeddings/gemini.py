"""Google Gemini embedding adapter (``gemini-embedding-001``) for the Embedder port.

Calls the REST ``:batchEmbedContents`` endpoint with httpx (already a runtime dep), pinning
``outputDimensionality`` to ``EMBED_DIM`` (1536) so vectors match the existing ``Vector(1536)``
columns. The API key is sent in the ``x-goog-api-key`` HTTP header (never in the URL) so it
cannot appear in httpx's INFO-level request logs.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from scrapeforge.core.embeddings.base import Embedder
from scrapeforge.core.embeddings.exceptions import (
    EmbeddingError,
    EmbeddingParseError,
    EmbeddingRateLimitError,
)
from scrapeforge.core.embeddings.settings import EmbedderSettings

log = logging.getLogger(__name__)


class GeminiEmbedder(Embedder):
    """Batches texts to Gemini's ``:batchEmbedContents`` and returns 1536-dim vectors."""

    def __init__(self, settings: EmbedderSettings) -> None:
        self._s = settings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self._s.EMBED_BATCH_SIZE):
            chunk = texts[start : start + self._s.EMBED_BATCH_SIZE]
            out.extend(await self._embed_chunk(chunk))
        return out

    async def _embed_chunk(self, chunk: list[str]) -> list[list[float]]:
        model = self._s.EMBED_MODEL
        url = f"{self._s.EMBED_API_BASE_URL.rstrip('/')}/models/{model}:batchEmbedContents"
        payload = {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": t}]},
                    "outputDimensionality": self._s.EMBED_DIM,
                }
                for t in chunk
            ]
        }
        data = await self._post_with_retry(url, payload)
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(chunk):
            raise EmbeddingParseError(
                f"expected {len(chunk)} embeddings, got "
                f"{len(embeddings) if isinstance(embeddings, list) else 'none'}"
            )
        vectors: list[list[float]] = []
        for item in embeddings:
            values = item.get("values") if isinstance(item, dict) else None
            if not isinstance(values, list) or not values:
                raise EmbeddingParseError("embedding item missing 'values'")
            vectors.append([float(x) for x in values])
        return vectors

    async def _post_with_retry(self, url: str, payload: dict) -> dict:
        headers = {"x-goog-api-key": self._s.EMBED_API_KEY}  # header, never in URL
        saw_429 = False
        async with httpx.AsyncClient(timeout=self._s.EMBED_REQUEST_TIMEOUT) as client:
            for attempt in range(self._s.EMBED_MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                except httpx.TimeoutException:
                    pass  # transient; retry, then fall through to EmbeddingError
                else:
                    if resp.status_code == 429:
                        saw_429 = True
                    elif resp.status_code >= 400:
                        raise EmbeddingError(f"embeddings HTTP {resp.status_code}")
                    else:
                        try:
                            return resp.json()
                        except json.JSONDecodeError as exc:
                            raise EmbeddingParseError("non-JSON embeddings response") from exc
                if attempt < self._s.EMBED_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
        if saw_429:
            raise EmbeddingRateLimitError("rate-limited after retries")
        raise EmbeddingError("embeddings request timed out after retries")
