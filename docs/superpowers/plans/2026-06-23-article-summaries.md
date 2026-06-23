# Phase 2: Article Summaries + Relevance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In one cheap LLM call per article, produce a 5-bullet investor summary **and** a 1–10 relevance score (from 5 criteria), stored on the article in Postgres.

**Architecture:** A provider-agnostic `Summarizer` port (default: free Zhipu GLM-4.5-Flash via an OpenAI-compatible `httpx` adapter, swappable by `.env`). A batch worker reads articles `WHERE summary IS NULL`, calls the summarizer with the owner's portfolio/interests, and writes `relevance` (indexed INT) + `summary` (JSONB) columns. No changes to the existing scrape/ingest pipeline.

**Tech Stack:** Python 3.12 async, `httpx` (+ `respx` for tests), SQLAlchemy 2.0 async + asyncpg + pgvector, `pydantic-settings`, pytest (`asyncio_mode=auto`), ruff. `@db` tests use an ephemeral pgvector container.

**Reference spec:** `docs/superpowers/specs/2026-06-23-article-summaries-design.md`

---

## Conventions for every task

- **TDD:** failing test → red → minimal impl → green → commit.
- **Gate before each commit:** `.venv/Scripts/python.exe -m ruff format <files>` then
  `.venv/Scripts/python.exe -m ruff check <files>` (0 errors).
- **`@db` tests** need pgvector. Start it once and export the URL:
  ```bash
  docker run -d --rm --name sf-pg -e POSTGRES_USER=scrapeforge -e POSTGRES_PASSWORD=scrapeforge \
    -e POSTGRES_DB=scrapeforge -p 5439:5432 pgvector/pgvector:pg16
  export DATABASE_URL="postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge"
  ```
- **Commit footer (every commit):** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Never** edit `engine.py`, `core/registry.py`, `core/db/repositories.py`, `exceptions.py`, the
  core `Settings` class, the root `cli.py`, or the existing scraper/transform/ingest workers.
- **No Alembic in this repo:** schema for `@db` tests is created by `Base.metadata.create_all`
  (see `tests/conftest.py`). Production gets the new columns via an idempotent
  `ADD COLUMN IF NOT EXISTS` helper (Task 4), called at the summarizer entry point.

## File map

| Task | Files |
|---|---|
| 1 | `scrapeforge/core/llm/__init__.py`, `exceptions.py`, `base.py` (new); `tests/test_llm_base.py` |
| 2 | `scrapeforge/core/llm/settings.py` (new); `tests/test_llm_settings.py` |
| 3 | `scrapeforge/core/llm/openai_compatible.py` (new); `tests/test_openai_compatible.py` |
| 4 | `scrapeforge/core/db/models.py` (+2 cols), `scrapeforge/core/db/migrations.py` (new); `tests/test_summary_columns.py` |
| 5 | `scrapeforge/worker/summarize_worker.py` (new); `tests/test_summarize_worker.py` |
| 6 | `scrapeforge/worker/run_summarize.py` (new), `deployment/docker-compose.yml`; `tests/test_run_summarize.py` |
| 7 | `tests/integration/test_summarize_live.py` (new, manual) |
| 8 | `SPEC.md`, `architecture.MD`, `planning.MD`, memory (no tests) |

---

## Task 1: LLM exceptions + `Summarizer` port + `SummaryResult`

**Files:**
- Create: `scrapeforge/core/llm/__init__.py` (empty), `scrapeforge/core/llm/exceptions.py`, `scrapeforge/core/llm/base.py`
- Test: `tests/test_llm_base.py`

- [ ] **Step 1: Write the failing test**

`tests/test_llm_base.py`:
```python
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
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_llm_base.py -q` → FAIL (import errors).

- [ ] **Step 3: Implement**

`scrapeforge/core/llm/__init__.py`:
```python
"""LLM summarization/scoring port + adapters (Phase 2)."""
```

`scrapeforge/core/llm/exceptions.py`:
```python
"""Typed LLM errors — the summarizer's own sub-hierarchy under ScrapeForgeError.

Defined in this module (not ``exceptions.py``) per the seam rule: subclass the base
hierarchy inside your feature. ``except LLMError`` catches every LLM failure.
"""

from __future__ import annotations

from scrapeforge.exceptions import ScrapeForgeError


class LLMError(ScrapeForgeError):
    """Any LLM provider/parse failure."""


class LLMRateLimitError(LLMError):
    """Provider rate-limited the request (HTTP 429) after retries were exhausted."""


class LLMParseError(LLMError):
    """The provider response could not be parsed into a valid SummaryResult."""
```

`scrapeforge/core/llm/base.py`:
```python
"""The Summarizer port: produce a 5-bullet summary + 1-10 relevance score for one article."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """The structured output of one summarize call.

    Attributes:
        bullets:   3-5 non-empty investor bullets (target 5; normalized by the adapter).
        relevance: overall 1-10 relevance-to-the-owner score.
        scores:    per-criterion 1-10 sub-scores
                   ({relevance, credibility, intensity, personal, time}).
        reason:    one-line rationale for the score.
        model:     the model id that produced this result.
    """

    bullets: list[str]
    relevance: int
    scores: dict[str, int]
    reason: str
    model: str


class Summarizer(ABC):
    """Provider-agnostic boundary the summarize worker depends on."""

    @abstractmethod
    async def summarize(
        self,
        *,
        title: str,
        content: str,
        published: datetime | None,
        portfolio: list[str],
        interests: list[str],
    ) -> SummaryResult:
        """Summarize + score one article for an investor with *portfolio*/*interests*."""
```

- [ ] **Step 4: Run green** — same command → PASS (3 passed).

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/core/llm/ tests/test_llm_base.py
.venv/Scripts/python.exe -m ruff check scrapeforge/core/llm/ tests/test_llm_base.py
git add scrapeforge/core/llm/ tests/test_llm_base.py
git commit -m "feat(llm): Summarizer port + SummaryResult + LLM exception hierarchy

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `SummarizerSettings` fragment

**Files:**
- Create: `scrapeforge/core/llm/settings.py`
- Test: `tests/test_llm_settings.py`

- [ ] **Step 1: Write the failing test**

`tests/test_llm_settings.py`:
```python
"""Tests for the per-module SummarizerSettings fragment (CSV parsing + defaults)."""

from __future__ import annotations


def test_defaults(fake_env) -> None:
    from scrapeforge.core.llm.settings import SummarizerSettings

    s = SummarizerSettings()
    assert s.SUMMARY_MODEL == "glm-4.5-flash"
    assert s.SUMMARY_BATCH_SIZE == 20
    assert s.SUMMARY_API_KEY == ""
    assert "z.ai" in s.SUMMARY_API_BASE_URL
    assert s.portfolio() == []
    assert s.interests() == []


def test_csv_parsing(monkeypatch, fake_env) -> None:
    from scrapeforge.core.llm.settings import SummarizerSettings

    monkeypatch.setenv("SUMMARY_PORTFOLIO", "Nvidia, TSMC ,, Anthropic")
    monkeypatch.setenv("SUMMARY_INTERESTS", "hybrid bonding, SpaceX IPO")
    s = SummarizerSettings()
    assert s.portfolio() == ["Nvidia", "TSMC", "Anthropic"]
    assert s.interests() == ["hybrid bonding", "SpaceX IPO"]
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_llm_settings.py -q` → FAIL.

- [ ] **Step 3: Implement** `scrapeforge/core/llm/settings.py` (mirrors `SubstackSettings`):
```python
"""Per-module configuration for the summarizer (SPEC.md Invariant #16 — never core Settings)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SummarizerSettings(BaseSettings):
    """LLM summarizer config. Overridable via environment / ``.env``.

    The default provider is the free Zhipu GLM-4.5-Flash on its OpenAI-compatible base
    URL; switch providers (DeepSeek/Qwen) by changing the three SUMMARY_API_* values.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SUMMARY_API_BASE_URL: str = Field(default="https://api.z.ai/api/paas/v4")
    SUMMARY_API_KEY: str = Field(default="")  # secret; .env only; empty => worker idles
    SUMMARY_MODEL: str = Field(default="glm-4.5-flash")
    SUMMARY_PORTFOLIO: str = Field(default="")  # CSV → criterion #1
    SUMMARY_INTERESTS: str = Field(default="")  # CSV → criterion #4
    SUMMARY_BATCH_SIZE: int = Field(default=20)
    SUMMARY_MAX_INPUT_CHARS: int = Field(default=12000)
    SUMMARY_REQUEST_TIMEOUT: float = Field(default=30.0)
    SUMMARY_INTER_REQUEST_DELAY: float = Field(default=1.0)
    SUMMARY_MAX_RETRIES: int = Field(default=2)  # 429/timeout retries before LLMRateLimitError

    def portfolio(self) -> list[str]:
        """Parse SUMMARY_PORTFOLIO (CSV) → clean list."""
        return [p.strip() for p in self.SUMMARY_PORTFOLIO.split(",") if p.strip()]

    def interests(self) -> list[str]:
        """Parse SUMMARY_INTERESTS (CSV) → clean list."""
        return [i.strip() for i in self.SUMMARY_INTERESTS.split(",") if i.strip()]
```

- [ ] **Step 4: Run green** — PASS (2 passed).

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/core/llm/settings.py tests/test_llm_settings.py
.venv/Scripts/python.exe -m ruff check scrapeforge/core/llm/settings.py tests/test_llm_settings.py
git add scrapeforge/core/llm/settings.py tests/test_llm_settings.py
git commit -m "feat(llm): SummarizerSettings fragment (provider + profile CSVs + batch knobs)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `OpenAICompatibleSummarizer` adapter

**Files:**
- Create: `scrapeforge/core/llm/openai_compatible.py`
- Test: `tests/test_openai_compatible.py`

- [ ] **Step 1: Write the failing tests** (`respx` mocks httpx; no network):

`tests/test_openai_compatible.py`:
```python
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
    base = {"SUMMARY_API_KEY": "secret-key", "SUMMARY_API_BASE_URL": _BASE, "SUMMARY_MAX_RETRIES": 1}
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
    assert out.scores == {"relevance": 9, "credibility": 8, "intensity": 7, "personal": 10, "time": 4}
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
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_openai_compatible.py -q` → FAIL.

- [ ] **Step 3: Implement** `scrapeforge/core/llm/openai_compatible.py`:
```python
"""OpenAI-compatible chat-completions adapter (GLM/DeepSeek/Qwen) for the Summarizer port."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

import httpx

from scrapeforge.core.llm.base import Summarizer, SummaryResult
from scrapeforge.core.llm.exceptions import LLMError, LLMParseError, LLMRateLimitError
from scrapeforge.core.llm.settings import SummarizerSettings

log = logging.getLogger(__name__)

_SCORE_KEYS = ("relevance", "credibility", "intensity", "personal", "time")


def _clamp_1_10(value: object) -> int:
    """Coerce *value* to an int in [1, 10]; raise LLMParseError if non-numeric."""
    try:
        n = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LLMParseError(f"non-numeric score: {value!r}") from exc
    return max(1, min(10, n))


def _loads(text: str) -> dict:
    """Parse a JSON object from *text*; fall back to the first {...} block."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise LLMParseError("no JSON object in model response") from None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"unparseable JSON: {exc}") from exc


def _build_messages(
    *, title: str, content: str, published: datetime | None,
    portfolio: list[str], interests: list[str], max_chars: int,
) -> list[dict]:
    today = datetime.now(UTC).date().isoformat()
    pub = published.date().isoformat() if published else "unknown"
    port = ", ".join(portfolio) or "(none given)"
    inter = ", ".join(interests) or "(none given)"
    system = (
        "You are an equity analyst scoring and summarizing articles for a specific investor. "
        f"The investor's portfolio: {port}. The investor's stated interests (including niche "
        f"topics): {inter}. Today's date is {today}; the article was published {pub}. "
        "Produce: (a) exactly 5 short bullets (<=25 words each) capturing the core claim/thesis, "
        "the company/ticker or sector, the key number or catalyst, the bull/bear angle, and why "
        "it matters; (b) integer 1-10 sub-scores for relevance (about AI, finance, the portfolio, "
        "or secular industry shifts), credibility (famous/respected Substack or leading "
        "researcher), intensity (fundraising / new investment / collaboration / technological "
        "breakthrough rank high), personal (matches the investor's stated interests, niche ones "
        "count), time (imminent/time-sensitive events rank high); (c) an overall relevance 1-10 "
        "weighing those for THIS investor; (d) a one-line reason. Return ONLY a JSON object: "
        '{"bullets":[5 strings],"scores":{"relevance":n,"credibility":n,"intensity":n,'
        '"personal":n,"time":n},"relevance":n,"reason":"..."}.'
    )
    user = f"Title: {title}\n\n{content[:max_chars]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class OpenAICompatibleSummarizer(Summarizer):
    """Calls any OpenAI-compatible /chat/completions endpoint and parses the result."""

    def __init__(self, settings: SummarizerSettings) -> None:
        self._s = settings

    async def summarize(
        self, *, title: str, content: str, published: datetime | None,
        portfolio: list[str], interests: list[str],
    ) -> SummaryResult:
        messages = _build_messages(
            title=title, content=content, published=published, portfolio=portfolio,
            interests=interests, max_chars=self._s.SUMMARY_MAX_INPUT_CHARS,
        )
        payload = {
            "model": self._s.SUMMARY_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {self._s.SUMMARY_API_KEY}"}
        url = f"{self._s.SUMMARY_API_BASE_URL.rstrip('/')}/chat/completions"

        text = await self._post_with_retry(url, payload, headers)
        return self._parse(text)

    async def _post_with_retry(self, url: str, payload: dict, headers: dict) -> str:
        last_status = None
        async with httpx.AsyncClient(timeout=self._s.SUMMARY_REQUEST_TIMEOUT) as client:
            for attempt in range(self._s.SUMMARY_MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    last_status = "timeout"
                else:
                    if resp.status_code == 429:
                        last_status = 429
                    elif resp.status_code >= 400:
                        raise LLMError(f"LLM HTTP {resp.status_code}")
                    else:
                        return resp.json()["choices"][0]["message"]["content"]
                if attempt < self._s.SUMMARY_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise LLMRateLimitError(f"rate-limited/timeout after retries (last={last_status})")

    def _parse(self, text: str) -> SummaryResult:
        obj = _loads(text)
        raw = obj.get("bullets")
        if not isinstance(raw, list):
            raise LLMParseError("missing 'bullets' array")
        bullets = [b.strip() for b in raw if isinstance(b, str) and b.strip()][:5]
        if len(bullets) < 3:
            raise LLMParseError(f"only {len(bullets)} usable bullets (need >=3)")
        raw_scores = obj.get("scores")
        if not isinstance(raw_scores, dict):
            raise LLMParseError("missing 'scores' object")
        scores = {k: _clamp_1_10(raw_scores.get(k)) for k in _SCORE_KEYS}
        relevance = _clamp_1_10(obj.get("relevance"))
        reason = str(obj.get("reason") or "").strip()
        return SummaryResult(
            bullets=bullets, relevance=relevance, scores=scores, reason=reason,
            model=self._s.SUMMARY_MODEL,
        )
```

- [ ] **Step 4: Run green** — PASS (6 passed).

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/core/llm/openai_compatible.py tests/test_openai_compatible.py
.venv/Scripts/python.exe -m ruff check scrapeforge/core/llm/openai_compatible.py tests/test_openai_compatible.py
git add scrapeforge/core/llm/openai_compatible.py tests/test_openai_compatible.py
git commit -m "feat(llm): OpenAI-compatible summarizer adapter (parse, clamp, retry, no key logging)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `relevance` + `summary` columns (+ idempotent prod migration)

**Files:**
- Modify: `scrapeforge/core/db/models.py` (add 2 columns to `Article`)
- Create: `scrapeforge/core/db/migrations.py`
- Test: `tests/test_summary_columns.py`

- [ ] **Step 1: Write the failing test**

`tests/test_summary_columns.py`:
```python
"""@db: the new relevance/summary columns round-trip; the prod migration is idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.session import make_sessionmaker


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


@pytest.mark.db
async def test_relevance_and_summary_roundtrip(db_session, session_factory) -> None:
    from scrapeforge.core.db.models import Article as ArticleRow

    async with session_factory() as s:
        s.add(
            ArticleRow(
                id="x" * 64, url="https://e.com/a", domain="e.com", bucket="community",
                title="T", content="C", author=None, publish_date=None,
                fetched_at=datetime.now(UTC), raw_key=None, meta={},
                relevance=8,
                summary={"bullets": ["a", "b", "c"], "scores": {"relevance": 9}, "reason": "r",
                         "model": "glm-4.5-flash", "generated_at": "2026-06-23T00:00:00+00:00"},
            )
        )
        await s.commit()

    row = await db_session.get(ArticleRow, "x" * 64)
    assert row.relevance == 8
    assert row.summary["bullets"] == ["a", "b", "c"]
    assert row.summary["scores"]["relevance"] == 9


@pytest.mark.db
async def test_ensure_columns_is_idempotent(_db_url) -> None:
    from scrapeforge.core.db.migrations import ensure_summary_columns

    engine = create_async_engine(_db_url, echo=False)
    await ensure_summary_columns(engine)
    await ensure_summary_columns(engine)  # second run must not raise
    await engine.dispose()
```

- [ ] **Step 2: Run red** (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_summary_columns.py -q -m db` → FAIL (`relevance`/`summary`/`ensure_summary_columns` don't exist).

- [ ] **Step 3: Implement the columns**

In `scrapeforge/core/db/models.py`, add to `class Article` (after the `embedding` column):
```python
    relevance: Mapped[int | None] = mapped_column(index=True, nullable=True)
    """AI relevance-to-owner score 1-10 (NULL until scored). Indexed for 'top by relevance'."""

    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    """{bullets: list[str], scores: dict, reason: str, model: str, generated_at: ISO-8601}. NULL until summarized."""
```
(`JSONB` and `Mapped`/`mapped_column` are already imported in this file.)

- [ ] **Step 4: Implement the idempotent prod migration**

`scrapeforge/core/db/migrations.py`:
```python
"""Schemaless-friendly column adds for environments without Alembic.

``@db`` tests get the schema from ``Base.metadata.create_all`` (conftest). An existing
production Postgres gets the Phase-2 columns via these idempotent ``ADD COLUMN IF NOT
EXISTS`` statements, called once at the summarizer entry point.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_STATEMENTS = (
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS relevance INTEGER",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS summary JSONB",
    "CREATE INDEX IF NOT EXISTS ix_articles_relevance ON articles (relevance)",
)


async def ensure_summary_columns(engine: AsyncEngine) -> None:
    """Idempotently add the relevance/summary columns + relevance index to ``articles``."""
    async with engine.begin() as conn:
        for stmt in _STATEMENTS:
            await conn.execute(text(stmt))
```

- [ ] **Step 5: Run green** — PASS (2 passed). Also run the existing model tests to confirm no
  regression: `.venv/Scripts/python.exe -m pytest tests/test_models.py tests/test_db_models.py -q -m db`.

- [ ] **Step 6: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/core/db/models.py scrapeforge/core/db/migrations.py tests/test_summary_columns.py
.venv/Scripts/python.exe -m ruff check scrapeforge/core/db/models.py scrapeforge/core/db/migrations.py tests/test_summary_columns.py
git add scrapeforge/core/db/models.py scrapeforge/core/db/migrations.py tests/test_summary_columns.py
git commit -m "feat(db): add relevance + summary columns to articles (+ idempotent prod migration)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `summarize_worker` (batch)

**Files:**
- Create: `scrapeforge/worker/summarize_worker.py`
- Test: `tests/test_summarize_worker.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_summarize_worker.py`:
```python
"""@db: the batch summarizer writes relevance+summary, is idempotent, paces, and skips on error."""

from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.llm.base import SummaryResult
from scrapeforge.core.llm.exceptions import LLMParseError, LLMRateLimitError


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


def _settings(**over):
    base = dict(SUMMARY_BATCH_SIZE=10, SUMMARY_INTER_REQUEST_DELAY=0.0,
                SUMMARY_PORTFOLIO_LIST=["Nvidia"], SUMMARY_INTERESTS_LIST=["hybrid bonding"])
    base.update(over)
    ns = types.SimpleNamespace(
        SUMMARY_BATCH_SIZE=base["SUMMARY_BATCH_SIZE"],
        SUMMARY_INTER_REQUEST_DELAY=base["SUMMARY_INTER_REQUEST_DELAY"],
    )
    ns.portfolio = lambda: base["SUMMARY_PORTFOLIO_LIST"]
    ns.interests = lambda: base["SUMMARY_INTERESTS_LIST"]
    return ns


class _FakeSummarizer:
    def __init__(self, *, raise_on=None, error=None):
        self.calls = []
        self._raise_on = raise_on or set()
        self._error = error

    async def summarize(self, *, title, content, published, portfolio, interests):
        self.calls.append((title, tuple(portfolio), tuple(interests), published))
        if title in self._raise_on:
            raise self._error
        return SummaryResult(
            bullets=["a", "b", "c", "d", "e"], relevance=7,
            scores={"relevance": 7, "credibility": 6, "intensity": 5, "personal": 8, "time": 4},
            reason="r", model="glm-4.5-flash",
        )


async def _add_article(session_factory, *, id_, title, summary=None):
    async with session_factory() as s:
        s.add(ArticleRow(
            id=id_, url=f"https://e.com/{id_}", domain="e.com", bucket="community",
            title=title, content="Body.", author=None, publish_date=None,
            fetched_at=datetime.now(UTC), raw_key=None, meta={}, summary=summary,
        ))
        await s.commit()


@pytest.mark.db
async def test_summarizes_only_unsummarized(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="New")
    await _add_article(session_factory, id_="b" * 64, title="Done",
                       summary={"bullets": ["x"], "model": "m"})

    fake = _FakeSummarizer()
    n = await summarize_pending(session_factory=session_factory, summarizer=fake, settings=_settings())
    assert n == 1
    assert fake.calls[0][1] == ("Nvidia",) and fake.calls[0][2] == ("hybrid bonding",)

    row = await db_session.get(ArticleRow, "a" * 64)
    assert row.relevance == 7
    assert row.summary["bullets"] == ["a", "b", "c", "d", "e"]
    assert row.summary["scores"]["personal"] == 8
    assert row.summary["model"] == "glm-4.5-flash"
    assert "generated_at" in row.summary


@pytest.mark.db
async def test_idempotent_rerun_does_nothing(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="New")
    s = _settings()
    assert await summarize_pending(session_factory=session_factory, summarizer=_FakeSummarizer(), settings=s) == 1
    assert await summarize_pending(session_factory=session_factory, summarizer=_FakeSummarizer(), settings=s) == 0


@pytest.mark.db
async def test_parse_error_skips_row_without_aborting(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="Bad")
    await _add_article(session_factory, id_="c" * 64, title="Good")
    fake = _FakeSummarizer(raise_on={"Bad"}, error=LLMParseError("nope"))

    n = await summarize_pending(session_factory=session_factory, summarizer=fake, settings=_settings())
    assert n == 1  # only "Good" persisted
    bad = await db_session.get(ArticleRow, "a" * 64)
    good = await db_session.get(ArticleRow, "c" * 64)
    assert bad.summary is None  # left NULL → retried later
    assert good.summary is not None


@pytest.mark.db
async def test_rate_limit_stops_run(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    await _add_article(session_factory, id_="a" * 64, title="RateLimited")
    await _add_article(session_factory, id_="c" * 64, title="NeverReached")
    fake = _FakeSummarizer(raise_on={"RateLimited"}, error=LLMRateLimitError("429"))

    n = await summarize_pending(session_factory=session_factory, summarizer=fake, settings=_settings())
    assert n == 0
    remaining = (await db_session.execute(
        select(ArticleRow).where(ArticleRow.summary.is_(None)))).scalars().all()
    assert len(remaining) == 2  # run stopped; nothing summarized


@pytest.mark.db
async def test_batch_size_caps_per_run(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import summarize_pending

    for i in range(3):
        await _add_article(session_factory, id_=str(i) * 64, title=f"A{i}")
    n = await summarize_pending(session_factory=session_factory, summarizer=_FakeSummarizer(),
                                settings=_settings(SUMMARY_BATCH_SIZE=2))
    assert n == 2


@pytest.mark.db
async def test_run_worker_drains_all(db_session, session_factory) -> None:
    from scrapeforge.worker.summarize_worker import run_summarize_worker

    for i in range(3):
        await _add_article(session_factory, id_=str(i) * 64, title=f"A{i}")
    await run_summarize_worker(session_factory=session_factory, summarizer=_FakeSummarizer(),
                               settings=_settings(SUMMARY_BATCH_SIZE=2))
    remaining = (await db_session.execute(
        select(ArticleRow).where(ArticleRow.summary.is_(None)))).scalars().all()
    assert remaining == []
```

- [ ] **Step 2: Run red** (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_summarize_worker.py -q -m db` → FAIL (module missing).

- [ ] **Step 3: Implement** `scrapeforge/worker/summarize_worker.py`:
```python
"""Batch summarizer worker (Phase 2): score + summarize articles WHERE summary IS NULL.

Reads a batch of un-summarized articles, calls the injected ``Summarizer``, and writes the
``relevance`` (int) + ``summary`` (JSONB) columns. Idempotent (the NULL gate), rate-limit
paced, and resilient (a per-article parse error skips that row; a rate-limit stops the run).
The query/update are inlined here (not added to ``repositories.py``) per the seam rules.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article
from scrapeforge.core.llm.base import Summarizer
from scrapeforge.core.llm.exceptions import LLMError, LLMRateLimitError

log = logging.getLogger(__name__)


async def summarize_pending(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    summarizer: Summarizer,
    settings,
) -> int:
    """Summarize one batch of un-summarized articles. Returns the count persisted."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Article)
                .where(Article.summary.is_(None))
                .order_by(Article.fetched_at.desc())
                .limit(settings.SUMMARY_BATCH_SIZE)
            )
        ).scalars().all()
        pending = [(r.id, r.title, r.content, r.publish_date) for r in rows]

    count = 0
    for article_id, title, content, published in pending:
        try:
            result = await summarizer.summarize(
                title=title, content=content, published=published,
                portfolio=settings.portfolio(), interests=settings.interests(),
            )
        except LLMRateLimitError:
            log.warning("summarize: rate-limited; stopping run after %d persisted", count)
            break
        except LLMError as exc:
            log.warning("summarize: skipping %s: %s", article_id, exc)
            continue

        async with session_factory() as session:
            await session.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(
                    relevance=result.relevance,
                    summary={
                        "bullets": result.bullets,
                        "scores": result.scores,
                        "reason": result.reason,
                        "model": result.model,
                        "generated_at": datetime.now(UTC).isoformat(),
                    },
                )
            )
            await session.commit()
        count += 1
        if settings.SUMMARY_INTER_REQUEST_DELAY:
            await asyncio.sleep(settings.SUMMARY_INTER_REQUEST_DELAY)

    return count


async def run_summarize_worker(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    summarizer: Summarizer,
    settings,
) -> None:
    """Drain all pending articles in successive batches until none remain."""
    while await summarize_pending(
        session_factory=session_factory, summarizer=summarizer, settings=settings
    ) > 0:
        pass
```

- [ ] **Step 4: Run green** — `.venv/Scripts/python.exe -m pytest tests/test_summarize_worker.py -q -m db` → PASS (6 passed).

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/worker/summarize_worker.py tests/test_summarize_worker.py
.venv/Scripts/python.exe -m ruff check scrapeforge/worker/summarize_worker.py tests/test_summarize_worker.py
git add scrapeforge/worker/summarize_worker.py tests/test_summarize_worker.py
git commit -m "feat(worker): batch summarizer (relevance+summary, idempotent, paced, resilient)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: entry point + compose service

**Files:**
- Create: `scrapeforge/worker/run_summarize.py`
- Modify: `deployment/docker-compose.yml`
- Test: `tests/test_run_summarize.py`

- [ ] **Step 1: Write the failing smoke test**

`tests/test_run_summarize.py`:
```python
"""Smoke test: the summarizer entry exposes an async main() and no-ops without a key."""

from __future__ import annotations

import inspect


def test_entry_exposes_async_main() -> None:
    from scrapeforge.worker import run_summarize

    assert inspect.iscoroutinefunction(run_summarize.main)
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_run_summarize.py -q` → FAIL.

- [ ] **Step 3: Implement** `scrapeforge/worker/run_summarize.py` (mirrors `run_transform.py`):
```python
"""Deployment entry point for the SUMMARIZER worker (Phase 2).

Builds the OpenAI-compatible summarizer + a DB session factory and drains un-summarized
articles in a poll loop. Empty SUMMARY_API_KEY => idle (no crash-loop, no spend). Run via
``python -m scrapeforge.worker.run_summarize``.
"""

from __future__ import annotations

import asyncio
import logging

from scrapeforge.config.settings import Settings
from scrapeforge.core.db.migrations import ensure_summary_columns
from scrapeforge.core.db.session import make_engine, make_sessionmaker
from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer
from scrapeforge.core.llm.settings import SummarizerSettings
from scrapeforge.worker.summarize_worker import run_summarize_worker

log = logging.getLogger(__name__)
_POLL_INTERVAL_S = 60.0


async def main() -> None:
    summarizer_settings = SummarizerSettings()
    engine = make_engine(Settings().DATABASE_URL)
    await ensure_summary_columns(engine)  # idempotent: self-heal the schema on existing DBs
    session_factory = make_sessionmaker(engine)

    if not summarizer_settings.SUMMARY_API_KEY:
        log.warning("SUMMARY_API_KEY is empty — summarizer idle (set it in .env to enable).")
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)

    summarizer = OpenAICompatibleSummarizer(summarizer_settings)
    while True:
        await run_summarize_worker(
            session_factory=session_factory, summarizer=summarizer, settings=summarizer_settings
        )
        await asyncio.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run green** — PASS (1 passed).

- [ ] **Step 5: Add the compose service**

In `deployment/docker-compose.yml`, after the `transform-worker` service block, add:
```yaml
  # --- Summarizer: scores + 5-bullet summaries for un-summarized articles via an OpenAI-
  #     compatible LLM (default free GLM-4.5-Flash). Reads/writes Postgres only. ---
  summarizer:
    build:
      context: ..
      dockerfile: deployment/Dockerfile.api
    command: ["python", "-m", "scrapeforge.worker.run_summarize"]
    environment:
      <<: *app-env
      SUMMARY_API_KEY: ${SUMMARY_API_KEY:-}
      SUMMARY_MODEL: ${SUMMARY_MODEL:-glm-4.5-flash}
      SUMMARY_API_BASE_URL: ${SUMMARY_API_BASE_URL:-https://api.z.ai/api/paas/v4}
      SUMMARY_PORTFOLIO: ${SUMMARY_PORTFOLIO:-}
      SUMMARY_INTERESTS: ${SUMMARY_INTERESTS:-}
    depends_on:
      postgres: { condition: service_healthy }
    restart: unless-stopped
```

- [ ] **Step 6: Verify compose parses**
```bash
POSTGRES_PASSWORD=x STATE_STORE_KEY=01234567890123456789012345678901234 API_KEYS=k API_DOMAIN=example.com \
  docker compose -f deployment/docker-compose.yml config >/dev/null && echo OK
```
Expected: `OK`.

- [ ] **Step 7: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/worker/run_summarize.py tests/test_run_summarize.py
.venv/Scripts/python.exe -m ruff check scrapeforge/worker/run_summarize.py tests/test_run_summarize.py
git add scrapeforge/worker/run_summarize.py tests/test_run_summarize.py deployment/docker-compose.yml
git commit -m "feat(deploy): summarizer entry point + compose service (idle without key)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: live `@integration` smoke (manual)

**Files:**
- Create: `tests/integration/test_summarize_live.py`

- [ ] **Step 1: Write the integration test**

`tests/integration/test_summarize_live.py`:
```python
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
```

- [ ] **Step 2: (Optional, needs a real key)** run it:
`SUMMARY_API_KEY=... .venv/Scripts/python.exe -m pytest -m integration tests/integration/test_summarize_live.py -v` — confirm 5 bullets + scores. Without a key it skips. (Never runs in CI.)

- [ ] **Step 3: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format tests/integration/test_summarize_live.py
.venv/Scripts/python.exe -m ruff check tests/integration/test_summarize_live.py
git add tests/integration/test_summarize_live.py
git commit -m "test(llm): live @integration smoke for the summarizer (manual, skips without key)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: docs + memory

**Files:** `SPEC.md`, `architecture.MD`, `planning.MD`, the project memory (no tests).

- [ ] **Step 1: SPEC.md** — in the module map / §3 area, document the `core/llm/` port + adapter
  and the two new `articles` columns (`relevance`, `summary`). Note the summarizer reads/writes
  Postgres directly (a batch read-modify-write, not the claim-check transform path) — consistent
  with the Phase-1 community-ingest carve-out (Invariant #18).

- [ ] **Step 2: architecture.MD** — add `core/llm/{base,openai_compatible,settings,exceptions}.py`,
  `core/db/migrations.py`, and `worker/{summarize_worker,run_summarize}.py` to the tree; add a
  short "Summarization (Phase 2)" note: `summarizer` polls `articles WHERE summary IS NULL` →
  OpenAI-compatible LLM (default free GLM-4.5-Flash) → writes `relevance` + `summary`.

- [ ] **Step 3: planning.MD** — mark Phase 2 (summaries + relevance) delivered; note next:
  Phase 2.5 (digest renders bullets + sorts by relevance), Phase 4 (swipe UI consumes the API).

- [ ] **Step 4: Memory** — add a `project` memory note: "Phase 2 summarizer — provider-agnostic
  OpenAI-compatible port; default free Zhipu GLM-4.5-Flash, swap via SUMMARY_API_* in .env;
  per-article 5 bullets + 1-10 relevance (5 criteria) scored against SUMMARY_PORTFOLIO/INTERESTS;
  stored in articles.relevance + articles.summary; batch worker on the NULL gate." Update the
  `MEMORY.md` pointer.

- [ ] **Step 5: Commit**
```bash
git add SPEC.md architecture.MD planning.MD
git commit -m "docs: document Phase-2 summarizer (llm port + relevance/summary columns)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (Definition of Done)

- [ ] `.venv/Scripts/python.exe -m ruff check .` → 0 errors
- [ ] `.venv/Scripts/python.exe -m ruff format --check .` → clean
- [ ] `.venv/Scripts/python.exe -m pytest -m "not integration" -q` (container + `DATABASE_URL`) →
      green incl. all new unit/`@db` tests; coverage ≥ 80%
- [ ] `docker compose -f deployment/docker-compose.yml config` parses with the `summarizer` service
- [ ] `SUMMARY_API_KEY` only ever in `.env` (gitignored) — never committed; CI never calls the API
- [ ] Push `feat/article-summaries`, open PR → `main`, CI green, squash-merge. Never push `main`.
- [ ] (Manual) set a real z.ai key and run the `@integration` smoke once to confirm end-to-end.
```
