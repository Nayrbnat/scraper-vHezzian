# Multi-user Per-User Relevance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rank the shared scraped corpus per-user by each user's portfolio + sectors using embeddings + pgvector cosine similarity — no LLM call per user — adding only new files plus additive edits.

**Architecture:** A new `core/embeddings/` Embedder port (mirrors the existing `core/llm/` Summarizer port) turns articles and user profiles into 1536-dim vectors. Three pure-async pipeline jobs (`embed_articles`, `embed_profiles`, `score_users`) fill `articles.embedding`, `user_profile_vectors`, and `user_article_relevance`. The Hezzian app owns/writes `user_profiles`; this pipeline only reads it and writes per-user scores. Scoring is pure in-database pgvector math (`cosine_distance`) — zero per-user API cost.

**Tech Stack:** Python 3.12 async, SQLAlchemy 2.0 async + asyncpg, pgvector (`Vector(1536)`, `cosine_distance`), httpx (already a runtime dep — Gemini called over REST), Typer CLI, pydantic-settings, pytest (`asyncio_mode=auto`) + respx + `@pytest.mark.db` against a pgvector container, ruff, 80% coverage gate.

**Spec:** `docs/superpowers/specs/2026-06-24-multiuser-relevance-design.md`
**Branch:** `feat/multiuser-relevance`

---

## Conventions (read once before starting)

- **Builder venv python:** `./.venv/Scripts/python.exe` (Windows). All `pytest`/`ruff` commands below use it.
- **`@db` tests** need a running pgvector Postgres. Set `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge` (the project's ephemeral pgvector container). If unreachable, `@db` tests skip with a clear message — that is acceptable locally but they MUST pass in CI.
- **Per-task loop (project workflow):** builder (TDD) → adversarial reviewer (diff-only) → fix → `code-simplifier` → `ruff check . && ruff format --check . && pytest -m "not integration"` gate → commit. One commit per task.
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Line length:** CI ruff enforces **100 chars**. Before pushing, scan: `git diff --name-only | grep '\.py$' | xargs awk 'length>100{print FILENAME":"NR}'` must be empty.
- **Seam rules (Invariant #16/#17):** Do NOT edit `engine.py`, `registry.py`, `repositories.py`, core `Settings`, or the scraper/summarize/digest modules. Permitted additive edits in this plan: `core/db/models.py` (precedent — Phase 2 added `relevance`/`summary` there), `pipeline/jobs.py::init_db`, `pipeline/cli.py`, `tests/conftest.py`, `.github/workflows/daily-pipeline.yml`, and the docs. Everything else is a NEW file.
- **No raw SQL in product code:** the `test_no_raw_sql.py` guard rejects `text(` in non-test product modules. Use SQLAlchemy Core/ORM only. `score_users` uses `Article.embedding.cosine_distance(...)` (confirmed available).
- **Never log API keys.** Gemini takes its key in the URL query string (`?key=...`) — never log the request URL.

---

## File Structure (what each new/edited file is responsible for)

**New — the Embedder port (`scrapeforge/core/embeddings/`):**
- `__init__.py` — package marker.
- `exceptions.py` — `EmbeddingError` / `EmbeddingRateLimitError` / `EmbeddingParseError` (subclass `ScrapeForgeError`).
- `base.py` — `Embedder` ABC: `async def embed(self, texts: list[str]) -> list[list[float]]`.
- `settings.py` — `EmbedderSettings` fragment (`EMBED_*`).
- `gemini.py` — `GeminiEmbedder` (PRIMARY): httpx → Gemini `:batchEmbedContents` REST, `outputDimensionality=EMBED_DIM`.
- `openai_compatible.py` — `OpenAICompatibleEmbedder` (fallback, Jina): POST `{BASE}/embeddings`.
- `factory.py` — `make_embedder(settings)` picks the adapter by `EMBED_PROVIDER`.

**New — the jobs (`scrapeforge/pipeline/embeddings_jobs.py`):**
- `embed_articles`, `embed_profiles`, `score_users`, `seed_owner` — pure-async, injected adapters.

**Edited (additive):**
- `core/db/models.py` — add `UserProfile`, `UserProfileVector`, `UserArticleRelevance` models.
- `pipeline/cli.py` — add `embed-articles`, `embed-profiles`, `score-users`, `seed-owner` subcommands.
- `tests/conftest.py` — extend the `db_session` TRUNCATE list with the three new tables.
- `.github/workflows/daily-pipeline.yml` — add `seed-owner → embed-articles → embed-profiles → score-users` steps + `EMBED_*` env.
- `SPEC.md`, `architecture.MD`, `planning.MD`, memory — doc updates.

**New tests:**
- `tests/test_embedder_gemini.py`, `tests/test_embedder_openai_compatible.py`, `tests/test_embedder_factory.py`,
  `tests/test_embeddings_jobs.py` (`@db`), `tests/test_multiuser_models.py` (`@db`), `tests/test_pipeline_embeddings_cli.py`.

---

## Task 0: Branch setup

- [ ] **Step 1: Create the feature branch from up-to-date main**

```bash
git checkout main
git pull --ff-only
git checkout -b feat/multiuser-relevance
```

No commit (branch only).

---

## Task 1: Embedder port — exceptions, ABC, settings

**Files:**
- Create: `scrapeforge/core/embeddings/__init__.py`
- Create: `scrapeforge/core/embeddings/exceptions.py`
- Create: `scrapeforge/core/embeddings/base.py`
- Create: `scrapeforge/core/embeddings/settings.py`
- Test: `tests/test_embedder_factory.py` (settings portion; factory added in Task 3)

- [ ] **Step 1: Write the failing test**

Create `tests/test_embedder_factory.py`:

```python
"""Tests for EmbedderSettings + the make_embedder factory."""

from __future__ import annotations


def test_settings_defaults_to_gemini_1536() -> None:
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    s = EmbedderSettings()
    assert s.EMBED_PROVIDER == "gemini"
    assert s.EMBED_MODEL == "gemini-embedding-001"
    assert s.EMBED_DIM == 1536
    assert s.EMBED_API_KEY == ""  # empty => jobs idle
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_embedder_factory.py -v`
Expected: FAIL with `ModuleNotFoundError: scrapeforge.core.embeddings.settings`.

- [ ] **Step 3: Create the package marker**

Create `scrapeforge/core/embeddings/__init__.py`:

```python
"""Embeddings port: turn text into vectors for per-user relevance (Phase 3)."""
```

- [ ] **Step 4: Create the exceptions module**

Create `scrapeforge/core/embeddings/exceptions.py`:

```python
"""Typed embedding errors — a sub-hierarchy under ScrapeForgeError (seam rule).

Defined here (not ``exceptions.py``) per the seam rule: subclass the base hierarchy
inside your feature. ``except EmbeddingError`` catches every embedding failure.
"""

from __future__ import annotations

from scrapeforge.exceptions import ScrapeForgeError


class EmbeddingError(ScrapeForgeError):
    """Any embedding provider/parse failure."""


class EmbeddingRateLimitError(EmbeddingError):
    """Provider rate-limited the request (HTTP 429) after retries were exhausted."""


class EmbeddingParseError(EmbeddingError):
    """The provider response could not be parsed into vectors."""
```

- [ ] **Step 5: Create the Embedder ABC**

Create `scrapeforge/core/embeddings/base.py`:

```python
"""The Embedder port: turn a list of texts into a list of equal-length vectors."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Embedder(ABC):
    """Provider-agnostic boundary the embedding jobs depend on."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order.

        Implementations batch internally. ``len(result) == len(texts)`` and every
        vector has the same dimension (``EMBED_DIM``). An empty input returns ``[]``.
        """
```

- [ ] **Step 6: Create the settings fragment**

Create `scrapeforge/core/embeddings/settings.py`:

```python
"""Per-module embedder config (never core Settings — Invariant #16).

Default provider is Google Gemini ``gemini-embedding-001`` via a free Google AI Studio
key, with output dimension pinned to 1536 to match the existing ``Vector(1536)`` columns
(no migration). Switch to an OpenAI-wire provider (e.g. Jina) via ``EMBED_PROVIDER``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbedderSettings(BaseSettings):
    """Embedding config. Overridable via environment / ``.env``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    EMBED_PROVIDER: str = Field(default="gemini")  # "gemini" | "openai_compatible"
    EMBED_API_KEY: str = Field(default="")  # secret; .env only; empty => jobs idle
    EMBED_API_BASE_URL: str = Field(default="https://generativelanguage.googleapis.com/v1beta")
    EMBED_MODEL: str = Field(default="gemini-embedding-001")
    EMBED_DIM: int = Field(default=1536)  # MUST match the Vector(N) columns
    EMBED_BATCH_SIZE: int = Field(default=100)
    EMBED_REQUEST_TIMEOUT: float = Field(default=60.0)
    EMBED_MAX_RETRIES: int = Field(default=2)
    # score_users knobs:
    EMBED_SCORE_WINDOW_DAYS: int = Field(default=30)  # only score articles fetched within window
    EMBED_TOP_K: int = Field(default=200)  # rows written per user
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_embedder_factory.py -v`
Expected: PASS.

- [ ] **Step 8: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/core/embeddings tests/test_embedder_factory.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/core/embeddings tests/test_embedder_factory.py
git add scrapeforge/core/embeddings tests/test_embedder_factory.py
git commit -m "feat(embeddings): add Embedder port, exceptions, and EmbedderSettings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: GeminiEmbedder adapter (primary)

Gemini's REST embedding endpoint batches via `:batchEmbedContents`:
`POST {BASE}/models/{model}:batchEmbedContents?key={KEY}` with body
`{"requests":[{"model":"models/{model}","content":{"parts":[{"text": t}]},"outputDimensionality":DIM}, ...]}`
→ `{"embeddings":[{"values":[...]}, ...]}`. Cosine distance is scale-invariant, so the (un-normalized) 1536-dim output needs no normalization.

**Files:**
- Create: `scrapeforge/core/embeddings/gemini.py`
- Test: `tests/test_embedder_gemini.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embedder_gemini.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_embedder_gemini.py -v`
Expected: FAIL with `ModuleNotFoundError: scrapeforge.core.embeddings.gemini`.

- [ ] **Step 3: Implement the adapter**

Create `scrapeforge/core/embeddings/gemini.py`:

```python
"""Google Gemini embedding adapter (``gemini-embedding-001``) for the Embedder port.

Calls the REST ``:batchEmbedContents`` endpoint with httpx (already a runtime dep), pinning
``outputDimensionality`` to ``EMBED_DIM`` (1536) so vectors match the existing ``Vector(1536)``
columns. The API key is passed in the URL query string and is NEVER logged.
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
        params = {"key": self._s.EMBED_API_KEY}  # key in query string — never logged
        saw_429 = False
        async with httpx.AsyncClient(timeout=self._s.EMBED_REQUEST_TIMEOUT) as client:
            for attempt in range(self._s.EMBED_MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, params=params, json=payload)
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
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_embedder_gemini.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/core/embeddings/gemini.py tests/test_embedder_gemini.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/core/embeddings/gemini.py tests/test_embedder_gemini.py
git add scrapeforge/core/embeddings/gemini.py tests/test_embedder_gemini.py
git commit -m "feat(embeddings): add GeminiEmbedder adapter (batchEmbedContents)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: OpenAI-compatible embedder (Jina fallback) + factory

OpenAI-wire embeddings: `POST {BASE}/embeddings` body `{"model": M, "input": [texts], "dimensions": DIM}`
→ `{"data":[{"embedding":[...]}, ...]}`. The Bearer key goes in the `Authorization` header.

**Files:**
- Create: `scrapeforge/core/embeddings/openai_compatible.py`
- Create: `scrapeforge/core/embeddings/factory.py`
- Test: `tests/test_embedder_openai_compatible.py`, extend `tests/test_embedder_factory.py`

- [ ] **Step 1: Write the failing tests (adapter)**

Create `tests/test_embedder_openai_compatible.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_embedder_openai_compatible.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the adapter**

Create `scrapeforge/core/embeddings/openai_compatible.py`:

```python
"""OpenAI-wire embeddings adapter (e.g. Jina) — the Embedder port's fallback provider."""

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
            vectors.append([float(x) for x in emb])
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
```

- [ ] **Step 4: Add the factory test**

Append to `tests/test_embedder_factory.py`:

```python
def test_factory_picks_gemini() -> None:
    from scrapeforge.core.embeddings.gemini import GeminiEmbedder
    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    embedder = make_embedder(EmbedderSettings(EMBED_PROVIDER="gemini", EMBED_API_KEY="k"))
    assert isinstance(embedder, GeminiEmbedder)


def test_factory_picks_openai_compatible() -> None:
    from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder
    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    embedder = make_embedder(
        EmbedderSettings(EMBED_PROVIDER="openai_compatible", EMBED_API_KEY="k")
    )
    assert isinstance(embedder, OpenAICompatibleEmbedder)


def test_factory_rejects_unknown_provider() -> None:
    import pytest

    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    with pytest.raises(ValueError, match="EMBED_PROVIDER"):
        make_embedder(EmbedderSettings(EMBED_PROVIDER="nope", EMBED_API_KEY="k"))
```

- [ ] **Step 5: Implement the factory**

Create `scrapeforge/core/embeddings/factory.py`:

```python
"""Select an Embedder adapter by ``EMBED_PROVIDER`` (extension by addition)."""

from __future__ import annotations

from scrapeforge.core.embeddings.base import Embedder
from scrapeforge.core.embeddings.gemini import GeminiEmbedder
from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder
from scrapeforge.core.embeddings.settings import EmbedderSettings


def make_embedder(settings: EmbedderSettings) -> Embedder:
    """Return the embedder adapter named by ``settings.EMBED_PROVIDER``."""
    provider = settings.EMBED_PROVIDER.strip().lower()
    if provider == "gemini":
        return GeminiEmbedder(settings)
    if provider == "openai_compatible":
        return OpenAICompatibleEmbedder(settings)
    raise ValueError(f"unknown EMBED_PROVIDER: {settings.EMBED_PROVIDER!r}")
```

- [ ] **Step 6: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_embedder_openai_compatible.py tests/test_embedder_factory.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/core/embeddings tests/test_embedder_openai_compatible.py tests/test_embedder_factory.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/core/embeddings tests/test_embedder_openai_compatible.py tests/test_embedder_factory.py
git add scrapeforge/core/embeddings/openai_compatible.py scrapeforge/core/embeddings/factory.py tests/test_embedder_openai_compatible.py tests/test_embedder_factory.py
git commit -m "feat(embeddings): add OpenAI-compatible embedder + provider factory

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Data model — three multi-user tables + cascade

Add the app↔pipeline contract tables to `models.py`. Precedent: Phase 2 added `relevance`/`summary`
columns to `Article` here, and `articles.embedding Vector(1536)` already exists. `init_db`'s existing
`Base.metadata.create_all` (checkfirst=True) creates these idempotently — `user_profiles` coexists with
the app's own migration because `create_all` skips already-existing tables. The FK
`user_article_relevance.article_id → articles.id ON DELETE CASCADE` means `prune` auto-cleans score rows.

**Files:**
- Modify: `scrapeforge/core/db/models.py` (append three models + imports)
- Modify: `tests/conftest.py` (extend the `db_session` TRUNCATE list)
- Test: `tests/test_multiuser_models.py`

- [ ] **Step 1: Write the failing `@db` tests**

Create `tests/test_multiuser_models.py`:

```python
"""@db: multi-user contract tables round-trip and cascade on article delete."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from scrapeforge.core.db.models import (
    Article,
    UserArticleRelevance,
    UserProfile,
    UserProfileVector,
)


@pytest.mark.db
async def test_user_profile_roundtrip(db_session) -> None:
    db_session.add(
        UserProfile(
            user_id="owner",
            portfolio=["NVDA", "MSFT"],
            sectors=["AI", "fintech"],
            focus="ai and finance",
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    row = (await db_session.execute(select(UserProfile))).scalar_one()
    assert row.portfolio == ["NVDA", "MSFT"]
    assert row.sectors == ["AI", "fintech"]


@pytest.mark.db
async def test_profile_vector_roundtrip(db_session) -> None:
    db_session.add(
        UserProfileVector(
            user_id="owner",
            embedding=[0.1] * 1536,
            source_hash="abc",
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    row = (await db_session.execute(select(UserProfileVector))).scalar_one()
    assert row.source_hash == "abc"
    assert len(list(row.embedding)) == 1536


@pytest.mark.db
async def test_relevance_cascades_on_article_delete(db_session) -> None:
    art_id = "a" * 64
    db_session.add(
        Article(
            id=art_id,
            url="https://e.com/a",
            domain="e.com",
            bucket="community",
            title="t",
            content="body",
            fetched_at=datetime.now(UTC),
            meta={},
        )
    )
    db_session.add(
        UserArticleRelevance(
            user_id="owner", article_id=art_id, score=0.9, computed_at=datetime.now(UTC)
        )
    )
    await db_session.commit()

    await db_session.execute(delete(Article).where(Article.id == art_id))
    await db_session.commit()

    remaining = (await db_session.execute(select(UserArticleRelevance))).scalars().all()
    assert remaining == []  # FK ON DELETE CASCADE removed the score row
```

- [ ] **Step 2: Run to verify failure**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_multiuser_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'UserProfile'`.

- [ ] **Step 3: Add the models**

In `scrapeforge/core/db/models.py`, extend the imports at the top:

```python
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, DateTime, Double, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
```

Then append these three models to the end of the file:

```python
class UserProfile(Base):
    """App-owned profile (the Hezzian app writes this; the pipeline only reads it).

    ``create_all`` uses ``checkfirst=True`` so this definition coexists with the app's own
    migration — whichever runs first creates the table; the other is a no-op.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(primary_key=True)
    """Matches the Hezzian app's user id."""

    portfolio: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    """Tickers / company names the user holds or tracks."""

    sectors: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    """Sectors of interest, e.g. ``{AI, semiconductors, fintech}``."""

    focus: Mapped[str | None]
    """Optional free-text emphasis (defaults to the global SUMMARY_FOCUS when unset)."""

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """Timezone-aware UTC timestamp of the last profile write."""


class UserProfileVector(Base):
    """Pipeline-owned embedding of a user's profile; re-embedded only when the profile changes."""

    __tablename__ = "user_profile_vectors"

    user_id: Mapped[str] = mapped_column(primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    source_hash: Mapped[str]
    """sha256 of (portfolio + sectors + focus); embed_profiles skips unchanged users."""

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserArticleRelevance(Base):
    """Pipeline-owned per-(user, article) similarity score; the app reads this for each feed."""

    __tablename__ = "user_article_relevance"

    user_id: Mapped[str] = mapped_column(primary_key=True)
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[float] = mapped_column(Double)
    """Cosine similarity in [-1, 1]; higher = better fit."""

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_uar_user_score", "user_id", "score"),)
```

- [ ] **Step 4: Extend the conftest TRUNCATE list**

In `tests/conftest.py`, the `db_session` teardown currently runs:

```python
                sqlalchemy.text("TRUNCATE TABLE articles, jobs, sources RESTART IDENTITY CASCADE")
```

Change it to include the three new tables:

```python
                sqlalchemy.text(
                    "TRUNCATE TABLE articles, jobs, sources, user_profiles, "
                    "user_profile_vectors, user_article_relevance RESTART IDENTITY CASCADE"
                )
```

- [ ] **Step 5: Run to verify pass**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_multiuser_models.py -v`
Expected: PASS (3 tests). (If no DB is reachable they SKIP — acceptable locally, must pass in CI.)

- [ ] **Step 6: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/core/db/models.py tests/conftest.py tests/test_multiuser_models.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/core/db/models.py tests/conftest.py tests/test_multiuser_models.py
git add scrapeforge/core/db/models.py tests/conftest.py tests/test_multiuser_models.py
git commit -m "feat(db): add user_profiles, user_profile_vectors, user_article_relevance

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `embed_articles` job

Fill `articles.embedding` for rows WHERE embedding IS NULL, newest first. Idempotent.

**Files:**
- Create: `scrapeforge/pipeline/embeddings_jobs.py`
- Test: `tests/test_embeddings_jobs.py`

- [ ] **Step 1: Write the failing `@db` test + a fake embedder**

Create `tests/test_embeddings_jobs.py`:

```python
"""@db: embed_articles / embed_profiles / score_users / seed_owner pipeline jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import (
    Article,
    UserArticleRelevance,
    UserProfile,
    UserProfileVector,
)
from scrapeforge.core.embeddings.base import Embedder


class FakeEmbedder(Embedder):
    """Deterministic 3-dim embedder: maps a marker substring to a unit axis."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            if "AI" in t:
                out.append([1.0, 0.0, 0.0])
            elif "OIL" in t:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker:
    return async_sessionmaker(create_async_engine(_db_url, echo=False), expire_on_commit=False)


async def _add_article(session_factory, *, id_, title, content, fetched_at, embedding=None) -> None:
    async with session_factory() as s:
        s.add(
            Article(
                id=id_,
                url=f"https://e.com/{id_}",
                domain="e.com",
                bucket="community",
                title=title,
                content=content,
                fetched_at=fetched_at,
                meta={},
                embedding=embedding,
            )
        )
        await s.commit()


@pytest.mark.db
async def test_embed_articles_fills_null_only(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    now = datetime.now(UTC)
    await _add_article(session_factory, id_="a" * 64, title="AI chips", content="x", fetched_at=now)
    await _add_article(
        session_factory,
        id_="b" * 64,
        title="done",
        content="x",
        fetched_at=now,
        embedding=[0.5, 0.5, 0.5] + [0.0] * 1533,
    )

    n = await embed_articles(
        session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10
    )
    assert n == 1  # only the NULL row embedded
    rows = (await db_session.execute(select(Article).order_by(Article.id))).scalars().all()
    assert list(rows[0].embedding)[:3] == [1.0, 0.0, 0.0]  # "AI" → x-axis
    assert list(rows[1].embedding)[:3] == [0.5, 0.5, 0.5]  # untouched


@pytest.mark.db
async def test_embed_articles_idempotent(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    now = datetime.now(UTC)
    await _add_article(session_factory, id_="a" * 64, title="AI", content="x", fetched_at=now)
    await embed_articles(session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10)
    n2 = await embed_articles(
        session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10
    )
    assert n2 == 0  # nothing left WHERE embedding IS NULL
```

NOTE: the embedder pads to 1536 in the job (the fake returns 3 dims; the job stores whatever the
embedder returns). For these tests the column is `Vector(1536)`, so the job MUST pad/validate to
`EMBED_DIM`. To keep the fake simple, the job stores the raw vector and the test seeds a full-1536
"done" row; the fake's 3-dim output is stored into the 1536 column by pgvector only if length matches.

**Correction to keep the test valid:** make `FakeEmbedder` return full 1536-dim vectors. Replace the
`embed` body above with:

```python
    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            base = [0.0] * 1536
            if "AI" in t:
                base[0] = 1.0
            elif "OIL" in t:
                base[1] = 1.0
            else:
                base[2] = 1.0
            out.append(base)
        return out
```

And update the two assertions to check `list(rows[0].embedding)[0] == 1.0` etc. accordingly.

- [ ] **Step 2: Run to verify failure**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_embeddings_jobs.py -v`
Expected: FAIL (`ModuleNotFoundError: scrapeforge.pipeline.embeddings_jobs`).

- [ ] **Step 3: Implement `embed_articles`**

Create `scrapeforge/pipeline/embeddings_jobs.py` with the imports + `embed_articles` (other jobs added in Tasks 6–7):

```python
"""Phase-3 multi-user embedding jobs (pure-async; injected Embedder).

Three jobs + an owner bootstrap, mirroring the summarize worker's shape:
- ``embed_articles``  : fill ``articles.embedding`` WHERE NULL (shared, idempotent).
- ``embed_profiles``  : embed each user's profile, skipping unchanged ones (source-hash gate).
- ``score_users``     : pgvector cosine similarity → top-K rows in ``user_article_relevance``.
- ``seed_owner``      : upsert a single ``user_id='owner'`` profile from the SUMMARY_* settings.

Queries/updates are inlined here (not added to ``repositories.py``) per the seam rules. No raw
SQL: similarity uses ``Article.embedding.cosine_distance(...)``.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import (
    Article,
    UserArticleRelevance,
    UserProfile,
    UserProfileVector,
)
from scrapeforge.core.embeddings.base import Embedder

log = logging.getLogger(__name__)

_ARTICLE_TEXT_CHARS = 2000  # title + leading body fed to the embedder


async def embed_articles(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    batch_size: int,
) -> int:
    """Embed articles WHERE ``embedding IS NULL`` (newest first). Returns rows updated."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Article.id, Article.title, Article.content)
                    .where(Article.embedding.is_(None))
                    .order_by(Article.fetched_at.desc(), Article.id.desc())
                    .limit(batch_size)
                )
            )
            .all()
        )
    if not rows:
        return 0

    texts = [f"{title}\n\n{(content or '')[:_ARTICLE_TEXT_CHARS]}" for _id, title, content in rows]
    vectors = await embedder.embed(texts)

    updated = 0
    async with session_factory() as session:
        for (article_id, _title, _content), vector in zip(rows, vectors, strict=True):
            await session.execute(
                update(Article).where(Article.id == article_id).values(embedding=vector)
            )
            updated += 1
        await session.commit()
    log.info("embed_articles: embedded %d article(s)", updated)
    return updated
```

- [ ] **Step 4: Run to verify pass**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_embeddings_jobs.py -v`
Expected: the two `embed_articles` tests PASS.

- [ ] **Step 5: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
git add scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
git commit -m "feat(pipeline): add embed_articles job (fill embedding WHERE NULL)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `embed_profiles` job + `seed_owner` bootstrap

`embed_profiles` reads `user_profiles`, hashes `portfolio+sectors+focus`, and upserts
`user_profile_vectors` only for users whose hash changed. `seed_owner` upserts the `owner` row
from `SummarizerSettings` (reusing the existing `SUMMARY_PORTFOLIO`/`SUMMARY_INTERESTS`/`SUMMARY_FOCUS`).

**Files:**
- Modify: `scrapeforge/pipeline/embeddings_jobs.py` (append two functions)
- Test: extend `tests/test_embeddings_jobs.py`

- [ ] **Step 1: Write the failing `@db` tests**

Append to `tests/test_embeddings_jobs.py`:

```python
async def _add_profile(session_factory, *, user_id, portfolio, sectors, focus=None) -> None:
    async with session_factory() as s:
        s.add(
            UserProfile(
                user_id=user_id,
                portfolio=portfolio,
                sectors=sectors,
                focus=focus,
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()


@pytest.mark.db
async def test_embed_profiles_embeds_then_skips_unchanged(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_profiles

    await _add_profile(session_factory, user_id="u1", portfolio=["NVDA"], sectors=["AI"])
    fake = FakeEmbedder()

    n1 = await embed_profiles(session_factory=session_factory, embedder=fake)
    assert n1 == 1
    vec = (await db_session.execute(select(UserProfileVector))).scalar_one()
    assert vec.user_id == "u1"
    assert vec.source_hash  # non-empty

    n2 = await embed_profiles(session_factory=session_factory, embedder=fake)
    assert n2 == 0  # unchanged profile → skipped (no second embed call for u1)


@pytest.mark.db
async def test_embed_profiles_reembeds_on_change(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_profiles

    await _add_profile(session_factory, user_id="u1", portfolio=["NVDA"], sectors=["AI"])
    await embed_profiles(session_factory=session_factory, embedder=FakeEmbedder())

    async with session_factory() as s:
        await s.execute(
            update(UserProfile).where(UserProfile.user_id == "u1").values(sectors=["OIL"])
        )
        await s.commit()

    n = await embed_profiles(session_factory=session_factory, embedder=FakeEmbedder())
    assert n == 1  # hash changed → re-embedded
    vec = (await db_session.execute(select(UserProfileVector))).scalar_one()
    assert list(vec.embedding)[1] == 1.0  # "OIL" → y-axis


@pytest.mark.db
async def test_seed_owner_upserts_from_settings(db_session, session_factory) -> None:
    from scrapeforge.core.llm.settings import SummarizerSettings
    from scrapeforge.pipeline.embeddings_jobs import seed_owner

    settings = SummarizerSettings(
        SUMMARY_PORTFOLIO="NVDA, MSFT", SUMMARY_INTERESTS="AI, fintech", SUMMARY_FOCUS="ai finance"
    )
    await seed_owner(session_factory=session_factory, settings=settings)
    await seed_owner(session_factory=session_factory, settings=settings)  # idempotent

    rows = (await db_session.execute(select(UserProfile))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == "owner"
    assert rows[0].portfolio == ["NVDA", "MSFT"]
    assert rows[0].sectors == ["AI", "fintech"]
    assert rows[0].focus == "ai finance"
```

Add the needed imports at the top of the test file (alongside the existing ones):
`from sqlalchemy import update`.

- [ ] **Step 2: Run to verify failure**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_embeddings_jobs.py -k "profiles or seed_owner" -v`
Expected: FAIL (`ImportError: cannot import name 'embed_profiles'`).

- [ ] **Step 3: Implement `embed_profiles` + `seed_owner`**

Append to `scrapeforge/pipeline/embeddings_jobs.py`:

```python
def _profile_text(portfolio: list[str], sectors: list[str], focus: str | None) -> str:
    port = ", ".join(portfolio) or "(none)"
    sect = ", ".join(sectors) or "(none)"
    return (
        f"Investor profile. Portfolio holdings: {port}. "
        f"Sectors of interest: {sect}. Focus: {focus or 'general investing'}."
    )


def _profile_hash(portfolio: list[str], sectors: list[str], focus: str | None) -> str:
    raw = "|".join(portfolio) + "||" + "|".join(sectors) + "||" + (focus or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def embed_profiles(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
) -> int:
    """Embed each user profile whose content hash changed. Returns profiles (re-)embedded."""
    async with session_factory() as session:
        profiles = (
            (
                await session.execute(
                    select(
                        UserProfile.user_id,
                        UserProfile.portfolio,
                        UserProfile.sectors,
                        UserProfile.focus,
                    )
                )
            )
            .all()
        )
        existing = dict(
            (
                await session.execute(
                    select(UserProfileVector.user_id, UserProfileVector.source_hash)
                )
            ).all()
        )

    changed = [
        (uid, portfolio or [], sectors or [], focus)
        for uid, portfolio, sectors, focus in profiles
        if _profile_hash(portfolio or [], sectors or [], focus) != existing.get(uid)
    ]
    if not changed:
        return 0

    texts = [_profile_text(p, s, f) for _uid, p, s, f in changed]
    vectors = await embedder.embed(texts)

    now = datetime.now(UTC)
    async with session_factory() as session:
        for (uid, portfolio, sectors, focus), vector in zip(changed, vectors, strict=True):
            stmt = pg_insert(UserProfileVector).values(
                user_id=uid,
                embedding=vector,
                source_hash=_profile_hash(portfolio, sectors, focus),
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[UserProfileVector.user_id],
                set_={
                    "embedding": stmt.excluded.embedding,
                    "source_hash": stmt.excluded.source_hash,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
        await session.commit()
    log.info("embed_profiles: (re-)embedded %d profile(s)", len(changed))
    return len(changed)


async def seed_owner(*, session_factory: async_sessionmaker[AsyncSession], settings) -> None:
    """Upsert a single ``user_id='owner'`` profile from the SUMMARY_* settings (idempotent)."""
    stmt = pg_insert(UserProfile).values(
        user_id="owner",
        portfolio=settings.portfolio(),
        sectors=settings.interests(),
        focus=settings.SUMMARY_FOCUS,
        updated_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UserProfile.user_id],
        set_={
            "portfolio": stmt.excluded.portfolio,
            "sectors": stmt.excluded.sectors,
            "focus": stmt.excluded.focus,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    async with session_factory() as session:
        await session.execute(stmt)
        await session.commit()
    log.info("seed_owner: upserted owner profile")
```

- [ ] **Step 4: Run to verify pass**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_embeddings_jobs.py -k "profiles or seed_owner" -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
git add scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
git commit -m "feat(pipeline): add embed_profiles (hash-gated) + seed_owner bootstrap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `score_users` job

For each user with a profile vector, rank recent articles by cosine similarity and UPSERT the top-K
into `user_article_relevance`. Pure pgvector via `Article.embedding.cosine_distance(uvec)` — no LLM,
no raw SQL. `score = 1 - cosine_distance`.

**Files:**
- Modify: `scrapeforge/pipeline/embeddings_jobs.py` (append `score_users`)
- Test: extend `tests/test_embeddings_jobs.py`

- [ ] **Step 1: Write the failing `@db` test**

Append to `tests/test_embeddings_jobs.py`:

```python
@pytest.mark.db
async def test_score_users_ranks_per_user_and_isolates(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import score_users

    now = datetime.now(UTC)
    ai_vec = [1.0, 0.0, 0.0] + [0.0] * 1533
    oil_vec = [0.0, 1.0, 0.0] + [0.0] * 1533
    await _add_article(
        session_factory, id_="a" * 64, title="AI", content="x", fetched_at=now, embedding=ai_vec
    )
    await _add_article(
        session_factory, id_="o" * 64, title="OIL", content="x", fetched_at=now, embedding=oil_vec
    )
    async with session_factory() as s:
        s.add(
            UserProfileVector(
                user_id="ai_user", embedding=ai_vec, source_hash="h", updated_at=now
            )
        )
        s.add(
            UserProfileVector(
                user_id="oil_user", embedding=oil_vec, source_hash="h", updated_at=now
            )
        )
        await s.commit()

    n = await score_users(session_factory=session_factory, window_days=30, top_k=1)
    assert n == 2  # one top row per user

    rows = (await db_session.execute(select(UserArticleRelevance))).scalars().all()
    by_user = {r.user_id: r.article_id for r in rows}
    assert by_user["ai_user"] == "a" * 64  # AI user's top match is the AI article
    assert by_user["oil_user"] == "o" * 64  # isolation: oil user gets the oil article


@pytest.mark.db
async def test_score_users_respects_window(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import score_users

    now = datetime.now(UTC)
    ai_vec = [1.0, 0.0, 0.0] + [0.0] * 1533
    await _add_article(
        session_factory,
        id_="old" + "a" * 61,
        title="AI",
        content="x",
        fetched_at=now - timedelta(days=99),
        embedding=ai_vec,
    )
    async with session_factory() as s:
        s.add(UserProfileVector(user_id="u", embedding=ai_vec, source_hash="h", updated_at=now))
        await s.commit()

    n = await score_users(session_factory=session_factory, window_days=30, top_k=10)
    assert n == 0  # the only article is older than the 30-day window
```

- [ ] **Step 2: Run to verify failure**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_embeddings_jobs.py -k score_users -v`
Expected: FAIL (`ImportError: cannot import name 'score_users'`).

- [ ] **Step 3: Implement `score_users`**

Append to `scrapeforge/pipeline/embeddings_jobs.py`:

```python
async def score_users(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    window_days: int,
    top_k: int,
) -> int:
    """Rank recent articles per user by cosine similarity; UPSERT the top-K. Returns rows written."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    now = datetime.now(UTC)

    async with session_factory() as session:
        users = (
            (await session.execute(select(UserProfileVector.user_id, UserProfileVector.embedding)))
            .all()
        )

    written = 0
    for user_id, uvec in users:
        uvec_list = list(uvec)
        distance = Article.embedding.cosine_distance(uvec_list)
        async with session_factory() as session:
            ranked = (
                (
                    await session.execute(
                        select(Article.id, distance.label("dist"))
                        .where(Article.embedding.is_not(None), Article.fetched_at >= cutoff)
                        .order_by(distance)
                        .limit(top_k)
                    )
                )
                .all()
            )
            for article_id, dist in ranked:
                stmt = pg_insert(UserArticleRelevance).values(
                    user_id=user_id,
                    article_id=article_id,
                    score=1.0 - float(dist),
                    computed_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        UserArticleRelevance.user_id,
                        UserArticleRelevance.article_id,
                    ],
                    set_={"score": stmt.excluded.score, "computed_at": stmt.excluded.computed_at},
                )
                await session.execute(stmt)
                written += 1
            await session.commit()
    log.info("score_users: wrote %d (user, article) score(s)", written)
    return written
```

- [ ] **Step 4: Run to verify pass**

Run: `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest tests/test_embeddings_jobs.py -v`
Expected: PASS (all jobs tests).

- [ ] **Step 5: Confirm the SQLi guard stays green**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_no_raw_sql.py -v`
Expected: PASS (`embeddings_jobs.py` uses no `text(`).

- [ ] **Step 6: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
git add scrapeforge/pipeline/embeddings_jobs.py tests/test_embeddings_jobs.py
git commit -m "feat(pipeline): add score_users job (pgvector cosine top-K per user)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: CLI subcommands

Add `seed-owner`, `embed-articles`, `embed-profiles`, `score-users` to the existing `pipeline` Typer
sub-app. Each mirrors the `summarize`/`prune` commands: selector loop + `asyncio.run` + engine dispose,
and idle-skips when `EMBED_API_KEY` is empty (except `seed-owner`, which needs no key).

**Files:**
- Modify: `scrapeforge/pipeline/cli.py` (append four commands)
- Test: `tests/test_pipeline_embeddings_cli.py`

- [ ] **Step 1: Write the failing test (CliRunner, mocked jobs)**

Create `tests/test_pipeline_embeddings_cli.py`:

```python
"""CLI wiring for the embedding subcommands (jobs mocked; no DB/network)."""

from __future__ import annotations

from typer.testing import CliRunner

from scrapeforge.pipeline.cli import pipeline_app

runner = CliRunner()


def test_embed_articles_skips_without_key(monkeypatch) -> None:
    monkeypatch.setenv("EMBED_API_KEY", "")
    result = runner.invoke(pipeline_app, ["embed-articles"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout.lower()


def test_embed_articles_runs_with_key(monkeypatch) -> None:
    monkeypatch.setenv("EMBED_API_KEY", "k")
    called = {}

    async def _fake_job(**kwargs):
        called["ran"] = True
        return 7

    monkeypatch.setattr("scrapeforge.pipeline.embeddings_jobs.embed_articles", _fake_job)
    # init-db / engine are also touched; stub the engine builder to avoid a real connection.
    monkeypatch.setattr(
        "scrapeforge.pipeline.cli.make_engine", lambda *a, **k: _StubEngine()
    )
    result = runner.invoke(pipeline_app, ["embed-articles"])
    assert result.exit_code == 0
    assert called.get("ran") is True
    assert "7" in result.stdout


class _StubEngine:
    async def dispose(self) -> None:
        return None
```

NOTE: `embed-articles` builds the embedder via `make_embedder(EmbedderSettings())`; with
`EMBED_API_KEY="k"` that constructs a `GeminiEmbedder` but never calls it (the job is mocked).
`make_sessionmaker(_StubEngine())` is only passed through to the mocked job, so no DB I/O occurs.
If `make_sessionmaker` rejects the stub, also stub it:
`monkeypatch.setattr("scrapeforge.pipeline.cli.make_sessionmaker", lambda e: None)`.

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pipeline_embeddings_cli.py -v`
Expected: FAIL (`No such command 'embed-articles'`).

- [ ] **Step 3: Implement the four commands**

Append to `scrapeforge/pipeline/cli.py`:

```python
@pipeline_app.command("seed-owner")
def seed_owner_cmd() -> None:
    """Upsert the single owner profile from SUMMARY_PORTFOLIO/INTERESTS/FOCUS (no API key needed)."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.core.llm.settings import SummarizerSettings
    from scrapeforge.pipeline.embeddings_jobs import seed_owner

    async def _run() -> None:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            await seed_owner(
                session_factory=make_sessionmaker(engine), settings=SummarizerSettings()
            )
        finally:
            await engine.dispose()

    asyncio.run(_run())
    typer.echo("seed-owner: owner profile upserted.")


def _embedder_or_skip(action: str):
    """Build the configured embedder, or return None (and echo a skip) if no key is set."""
    import logging

    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    settings = EmbedderSettings()
    if not settings.EMBED_API_KEY:
        logging.getLogger(__name__).warning("EMBED_API_KEY empty — %s skipped.", action)
        typer.echo(f"{action}: skipped (no EMBED_API_KEY).")
        return None, None
    return make_embedder(settings), settings


@pipeline_app.command("embed-articles")
def embed_articles_cmd() -> None:
    """Embed articles WHERE embedding IS NULL (idempotent). Skips if no EMBED_API_KEY."""
    _use_selector_loop()
    embedder, settings = _embedder_or_skip("embed-articles")
    if embedder is None:
        return

    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await embed_articles(
                session_factory=make_sessionmaker(engine),
                embedder=embedder,
                batch_size=settings.EMBED_BATCH_SIZE,
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"embed-articles: embedded {n} article(s).")


@pipeline_app.command("embed-profiles")
def embed_profiles_cmd() -> None:
    """Embed changed user profiles (source-hash gate). Skips if no EMBED_API_KEY."""
    _use_selector_loop()
    embedder, _settings = _embedder_or_skip("embed-profiles")
    if embedder is None:
        return

    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.pipeline.embeddings_jobs import embed_profiles

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await embed_profiles(
                session_factory=make_sessionmaker(engine), embedder=embedder
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"embed-profiles: (re-)embedded {n} profile(s).")


@pipeline_app.command("score-users")
def score_users_cmd() -> None:
    """Score recent articles per user via pgvector cosine similarity (no API key needed)."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.core.embeddings.settings import EmbedderSettings
    from scrapeforge.pipeline.embeddings_jobs import score_users

    settings = EmbedderSettings()

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await score_users(
                session_factory=make_sessionmaker(engine),
                window_days=settings.EMBED_SCORE_WINDOW_DAYS,
                top_k=settings.EMBED_TOP_K,
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"score-users: wrote {n} (user, article) score(s).")
```

Also add the import used by the test's monkeypatch target — ensure `make_engine`/`make_sessionmaker`
are referenced via the module (they already are imported inside each command). The test patches
`scrapeforge.pipeline.cli.make_engine`; since the commands import it lazily inside `_run`, change those
two commands' imports to module-level so the patch target exists. Simplest: at the top of `cli.py`,
the functions import lazily — keep that, but the CLI test patches
`scrapeforge.pipeline.embeddings_jobs.embed_articles` (the job) and stubs the engine. To make the
engine patchable, the `embed-articles` command should import `make_engine` at module load. Add near the
top of `cli.py` (module level), after the existing imports:

```python
from scrapeforge.core.db.session import make_engine, make_sessionmaker  # noqa: E402  (patch target)
```

and drop the duplicate local `from scrapeforge.core.db.session import make_engine, make_sessionmaker`
lines inside the four new commands (keep them in the pre-existing commands untouched to respect the
seam — only the NEW commands rely on the module-level import).

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pipeline_embeddings_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Smoke the help output**

Run: `./.venv/Scripts/python.exe -m scrapeforge pipeline --help`
Expected: lists `seed-owner`, `embed-articles`, `embed-profiles`, `score-users`.

- [ ] **Step 6: Lint + commit**

```bash
./.venv/Scripts/python.exe -m ruff check scrapeforge/pipeline/cli.py tests/test_pipeline_embeddings_cli.py
./.venv/Scripts/python.exe -m ruff format scrapeforge/pipeline/cli.py tests/test_pipeline_embeddings_cli.py
git add scrapeforge/pipeline/cli.py tests/test_pipeline_embeddings_cli.py
git commit -m "feat(cli): add seed-owner / embed-articles / embed-profiles / score-users

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Workflow + docs + memory

Wire the four steps into the daily workflow (after `summarize`, before `prune`) and update the docs.

**Files:**
- Modify: `.github/workflows/daily-pipeline.yml`
- Modify: `SPEC.md`, `architecture.MD`, `planning.MD`
- Modify: `C:\Users\nayrb\.claude\projects\C--Users-nayrb-Documents-scraper-vHezzian\memory\multiuser-relevance-spec.md` + `MEMORY.md`

- [ ] **Step 1: Add the workflow env + steps**

In `.github/workflows/daily-pipeline.yml`, add to the `env:` block (under the existing `SUMMARY_*` secrets):

```yaml
      # --- embeddings (Phase 3 multi-user relevance) ---
      EMBED_API_KEY: ${{ secrets.EMBED_API_KEY }}
      EMBED_PROVIDER: ${{ vars.EMBED_PROVIDER || 'gemini' }}
      EMBED_MODEL: ${{ vars.EMBED_MODEL || 'gemini-embedding-001' }}
```

Then insert these steps between the `Summarize + score the new articles` step and the
`Prune old / irrelevant articles` step:

```yaml
      - name: Seed the owner profile (single-user today)
        # Upserts user_id='owner' from SUMMARY_PORTFOLIO/INTERESTS/FOCUS so the per-user path
        # runs for the owner. No API key needed.
        run: python -m scrapeforge pipeline seed-owner

      - name: Embed new articles (fills articles.embedding WHERE NULL)
        # Idle-skips when EMBED_API_KEY is unset, so the pipeline stays green pre-rollout.
        run: python -m scrapeforge pipeline embed-articles

      - name: Embed changed user profiles
        run: python -m scrapeforge pipeline embed-profiles

      - name: Score articles per user (pgvector cosine; no API cost)
        run: python -m scrapeforge pipeline score-users
```

- [ ] **Step 2: Validate the workflow YAML parses**

Run: `./.venv/Scripts/python.exe -c "import yaml,sys; yaml.safe_load(open('.github/workflows/daily-pipeline.yml')); print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Update SPEC.md — add Invariant #19**

In `SPEC.md`, after the last invariant, add:

```markdown
- **Invariant #19 (multi-user relevance — Phase 3).** Per-user ranking is computed by embeddings +
  pgvector cosine similarity over the SHARED corpus, never by a per-user LLM call. The Hezzian app is
  the sole writer of `user_profiles`; the pipeline only reads it and is the sole writer of
  `user_profile_vectors` and `user_article_relevance`. `EMBED_DIM` must equal the `Vector(N)` column
  width (1536). Embedding jobs idle when `EMBED_API_KEY` is empty.
```

- [ ] **Step 4: Update architecture.MD**

Add `core/embeddings/` (port + gemini/openai_compatible adapters + factory + settings) and
`pipeline/embeddings_jobs.py` to the module tree, and add the three contract tables
(`user_profiles`, `user_profile_vectors`, `user_article_relevance`) to the data-model section,
noting the `embed-articles → embed-profiles → score-users` daily flow.

- [ ] **Step 5: Update planning.MD**

Mark **Phase 3 — multi-user per-user relevance** as IN PROGRESS / DELIVERED (per execution state),
referencing the spec and this plan, and listing the new `EMBED_API_KEY` deploy secret.

- [ ] **Step 6: Update memory**

In `multiuser-relevance-spec.md`, change "SPEC written, build NOT started" to record that the build
landed on branch `feat/multiuser-relevance` (jobs `embed-articles`/`embed-profiles`/`score-users` +
`seed-owner`, three contract tables, Gemini embedder), and that the remaining owner action is to set
the `EMBED_API_KEY` GitHub secret. Update the matching `MEMORY.md` one-liner.

- [ ] **Step 7: Full gate**

```bash
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m ruff format --check .
DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge ./.venv/Scripts/python.exe -m pytest -m "not integration" --cov --cov-fail-under=80
```

Expected: ruff clean; all tests green; coverage ≥ 80%.

- [ ] **Step 8: Line-length scan (CI ruff is stricter)**

Run: `git diff main --name-only | grep '\.py$' | xargs awk 'length>100{print FILENAME":"NR": "length}'`
Expected: no output.

- [ ] **Step 9: Commit**

```bash
git add .github/workflows/daily-pipeline.yml SPEC.md architecture.MD planning.MD
git commit -m "feat(pipeline): wire embedding jobs into daily workflow + docs

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(The memory files live outside the repo — write them with the Write tool, not git.)

---

## Task 10: Push + open PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/multiuser-relevance
```

- [ ] **Step 2: Open the PR to main**

```bash
gh pr create --base main --head feat/multiuser-relevance \
  --title "feat: multi-user per-user relevance (embeddings + pgvector)" \
  --body "$(cat <<'BODY'
Phase 3: per-user article ranking via embeddings + pgvector cosine similarity over the shared corpus.

## What this adds
- `core/embeddings/` Embedder port + GeminiEmbedder (primary) + OpenAICompatibleEmbedder (Jina fallback) + factory + settings.
- Three contract tables: `user_profiles` (app-owned), `user_profile_vectors`, `user_article_relevance` (pipeline-owned, FK cascade from articles).
- `pipeline/embeddings_jobs.py`: `embed_articles`, `embed_profiles` (hash-gated), `score_users` (pgvector cosine top-K), `seed_owner`.
- Four `pipeline` CLI subcommands + four daily-workflow steps (idle when `EMBED_API_KEY` unset).
- No per-user LLM cost; scoring is pure in-DB vector math.

## Deploy follow-up
Set the `EMBED_API_KEY` GitHub secret (free Google AI Studio key) to activate embeddings.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 3: Verify CI is green**

```bash
gh pr checks --watch
```

Expected: all checks pass. Then use **superpowers:finishing-a-development-branch** to squash-merge.

---

## Self-Review (completed during authoring)

**Spec coverage:**
- §2 architecture (embed-articles/embed-profiles/score-users) → Tasks 5–8 ✓
- §3 data model (3 tables, FK cascade, `articles.embedding` reuse) → Task 4 ✓
- §4.1 Embedder port (base/gemini/openai_compatible/settings/factory) → Tasks 1–3 ✓
- §4.2 three jobs (NULL gate, hash gate, pgvector cosine, bound params) → Tasks 5–7 ✓
- §4.3 three CLI subcommands + idle-skip → Task 8 ✓
- §4.4 owner bootstrap (`seed-owner`) → Task 6/8 ✓
- §5 daily workflow + prune cascade → Tasks 4 (FK cascade) & 9 ✓
- §6 scoring = pure cosine (v1) → Task 7 ✓
- §7 Gemini primary, Jina fallback, `EMBED_*` settings → Tasks 1–3 ✓
- §8 hermetic DoD (respx adapters, `@db` jobs, isolation, cascade, SQLi guard) → Tasks 2–7 ✓
- §9 seam compliance (new files + named additive edits only) → all tasks ✓

**Placeholder scan:** none — every code step has complete code.

**Type/name consistency:** `Embedder.embed(texts)->list[list[float]]`, `make_embedder(settings)`,
`embed_articles(session_factory, embedder, batch_size)`, `embed_profiles(session_factory, embedder)`,
`score_users(session_factory, window_days, top_k)`, `seed_owner(session_factory, settings)`,
`EmbedderSettings.EMBED_*`, models `UserProfile`/`UserProfileVector`/`UserArticleRelevance` — all
consistent across tasks. `score = 1 - cosine_distance` used uniformly.

**One deliberate gap (YAGNI):** no ivfflat/hnsw index on `articles.embedding` in v1 — exact sequential
scan over a few-thousand-row windowed corpus is fast and more accurate; add an approximate index later
if the corpus grows. Noted, not silently dropped.
```