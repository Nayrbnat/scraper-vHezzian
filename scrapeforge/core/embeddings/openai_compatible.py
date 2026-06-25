"""OpenAI-wire embeddings adapter (e.g. Jina) — the Embedder port's fallback provider."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from scrapeforge.core.embeddings._vectors import finalize_vector
from scrapeforge.core.embeddings.base import Embedder
from scrapeforge.core.embeddings.exceptions import (
    EmbeddingError,
    EmbeddingParseError,
    EmbeddingRateLimitError,
)
from scrapeforge.core.embeddings.settings import EmbedderSettings

log = logging.getLogger(__name__)


class OpenAICompatibleEmbedder(Embedder):
    """POSTs to any OpenAI-compatible ``/embeddings`` endpoint and parses the result."""

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
        url = f"{self._s.EMBED_API_BASE_URL.rstrip('/')}/embeddings"
        payload = {
            "model": self._s.EMBED_MODEL,
            "input": chunk,
            "dimensions": self._s.EMBED_DIM,
        }
        headers = {"Authorization": f"Bearer {self._s.EMBED_API_KEY}"}
        data = await self._post_with_retry(url, payload, headers)
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != len(chunk):
            raise EmbeddingParseError(
                f"expected {len(chunk)} embeddings, got "
                f"{len(rows) if isinstance(rows, list) else 'none'}"
            )
        vectors: list[list[float]] = []
        for item in rows:
            emb = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(emb, list) or not emb:
                raise EmbeddingParseError("embedding item missing 'embedding'")
            vectors.append(finalize_vector([float(x) for x in emb], self._s.EMBED_DIM))
        return vectors

    async def _post_with_retry(self, url: str, payload: dict, headers: dict) -> dict:
        saw_429 = False
        async with httpx.AsyncClient(timeout=self._s.EMBED_REQUEST_TIMEOUT) as client:
            for attempt in range(self._s.EMBED_MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    pass
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
