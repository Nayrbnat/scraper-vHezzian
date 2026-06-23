# Design — Phase 2: per-article AI 5-bullet summaries

- **Date:** 2026-06-23
- **Status:** approved (brainstorming → spec)
- **Branch:** `feat/article-summaries`
- **Builds on:** Phase 1 community ingestion (articles land in Postgres with `summary = NULL`).

## 1. Context & goal

Phase 1 lands parsed Substack articles in the `articles` table (`id=sha256(url)`,
`title/content/author/publish_date/bucket/meta JSONB`, `embedding Vector(1536) NULL`).

**Goal:** every article gets a stored **5-bullet "is this worth my time?" summary**, generated
by a cheap **non-Claude** model (default: the **free Zhipu GLM-4.5-Flash**), behind a
provider-agnostic port so the model is a `.env` swap (DeepSeek / Qwen — all OpenAI-compatible).
Summaries are produced by a **batch worker** and stored on the article, ready for the future
swipe UI and (Phase 2.5) the email digest.

**User constraints:** do NOT use Claude/Anthropic. Use a Chinese ultra-cheap/free API. Cost must
be "practically free" — the free GLM-4.5-Flash tier achieves $0/day within its rate limits.

## 2. Decisions (all confirmed in brainstorming)

| Decision | Choice |
|---|---|
| Provider (default) | **Zhipu GLM-4.5-Flash** (free), via an OpenAI-compatible port → swappable by config |
| Trigger | **Batch worker**: `SELECT … WHERE summary IS NULL`, rate-limit paced, idempotent, backfills |
| Storage | New nullable **`summary` JSONB column** on `articles`: `{bullets: [5], model, generated_at}` |
| Cadence | The summarizer is **its own background worker** (own poll loop + compose service) |
| Digest integration | **Out of scope for Phase 2** — it's Phase 2.5 (digest needs a Postgres source + async plumbing) |

## 3. Components (each one responsibility / interface / deps)

### 3.1 `core/llm/base.py` — the `Summarizer` port
- **Responsibility:** the boundary the worker depends on; hides the provider.
- **Interface:**
  ```python
  @dataclass(frozen=True, slots=True)
  class SummaryResult:
      bullets: list[str]   # 3–5 non-empty bullets (target 5; validated by the adapter)
      model: str

  class Summarizer(ABC):
      @abstractmethod
      async def summarize(self, *, title: str, content: str) -> SummaryResult: ...
  ```
- **Deps:** none (pure ABC + dataclass). Mockable with a fake in tests.

### 3.2 `core/llm/openai_compatible.py` — `OpenAICompatibleSummarizer(Summarizer)`
- **Responsibility:** translate one `summarize` call into an OpenAI-compatible
  `POST {base_url}/chat/completions` and parse the reply into 5 bullets.
- **Behaviour:** async `httpx` client; sends a system + user message (see §4) asking for a JSON
  **object** `{"bullets": [...]}` (compatible with `response_format={"type":"json_object"}` when
  the provider supports it; harmless otherwise). Parse: `json.loads(...)["bullets"]` first;
  **fallback** to line-parsing the raw text (strip `-`/`*`/`1.` markers). Normalize: drop empty
  bullets, keep the first 5; require **≥ 3** usable bullets else raise `LLMParseError`. Map HTTP
  429 / timeouts → bounded retry with backoff, then raise `LLMRateLimitError`; other non-2xx →
  `LLMError`. No secrets logged.
- **Deps:** `httpx` (already a dep), `SummarizerSettings`, the `LLMError` hierarchy.

### 3.3 `core/llm/settings.py` — `SummarizerSettings` (per-module fragment)
- Per-module `BaseSettings` (NOT core `Settings`, per Invariant #16). Reads the same `.env`.
- Fields:
  - `SUMMARY_API_BASE_URL: str = "https://api.z.ai/api/paas/v4"` (z.ai OpenAI-compatible base —
    **verify the exact base/path against current z.ai docs at build time**; it is config-driven).
  - `SUMMARY_API_KEY: str = ""` (secret; `.env` only; empty ⇒ worker no-ops with a clear warning).
  - `SUMMARY_MODEL: str = "glm-4.5-flash"`.
  - `SUMMARY_BATCH_SIZE: int = 20` (max articles per worker run).
  - `SUMMARY_MAX_INPUT_CHARS: int = 12000` (truncate long article bodies before sending).
  - `SUMMARY_REQUEST_TIMEOUT: float = 30.0`.
  - `SUMMARY_INTER_REQUEST_DELAY: float = 1.0` (politeness/pacing between calls).

### 3.4 `core/llm/exceptions.py` — typed errors
`LLMError(ScrapeForgeError)`; `LLMRateLimitError(LLMError)`; `LLMParseError(LLMError)`. No bare
excepts anywhere.

### 3.5 `core/db/models.py` — `summary` column (additive)
Add to `Article`:
```python
summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
"""AI 5-bullet summary: {bullets: list[str], model: str, generated_at: ISO-8601}. NULL until summarized."""
```
Plus an **Alembic migration** (`ALTER TABLE articles ADD COLUMN summary JSONB`). `@db` tests use
`create_all`, so the column appears automatically in tests; the migration is for production.

### 3.6 `worker/summarize_worker.py` — the batch worker
- **Responsibility:** summarize a batch of un-summarized articles.
- **Interface:**
  - `async def summarize_pending(*, session_factory, summarizer: Summarizer, settings) -> int`
    — returns the number of articles summarized this run.
  - `async def run_summarize_worker(*, session_factory, summarizer, settings) -> None`
    — poll loop wrapper (drain one batch, sleep, repeat) for the deployment entry.
- **Behaviour:** `SELECT * FROM articles WHERE summary IS NULL ORDER BY fetched_at DESC LIMIT
  SUMMARY_BATCH_SIZE` (query inlined here, not in `repositories.py`). For each row: build the
  prompt from `title` + truncated `content`; `await summarizer.summarize(...)`; on success
  `UPDATE articles SET summary = {bullets, model, generated_at=now(UTC)} WHERE id = …`; count++.
  **Pacing/errors:** sleep `SUMMARY_INTER_REQUEST_DELAY` between calls; on `LLMRateLimitError`
  **stop the run** (leave the rest NULL → next tick resumes); on `LLMParseError`/other per-article
  error, **log and skip** that article (stays NULL → retried later), do not abort the batch.
- **Deps:** `session_factory`, an injected `Summarizer`, `SummarizerSettings`.

### 3.7 `worker/run_summarize.py` + deployment
- Entry point mirroring `run_transform.py`: build `Settings`/`SummarizerSettings`, construct the
  `OpenAICompatibleSummarizer`, `make_sessionmaker`, run `run_summarize_worker` in a poll loop.
  If `SUMMARY_API_KEY` is empty, log a clear warning and idle (don't crash-loop).
- `deployment/docker-compose.yml`: add a `summarizer` service (Dockerfile.api, depends_on
  postgres; needs `SUMMARY_*` env). `docker compose config` must parse.

## 4. Prompt (investor "worth my time?" focus)

System: *"You are an equity analyst writing for a busy investor. Read the article and output
exactly 5 short bullets (≤ 25 words each) that let the reader decide if it's worth their time:
(1) the core claim/thesis, (2) the company/ticker or sector in focus, (3) the key number,
catalyst, or data point, (4) the bull or bear angle / what's contrarian, (5) why it matters for
an investment decision. Return ONLY a JSON object of the form
{"bullets": ["…","…","…","…","…"]} with exactly 5 strings."*

User: `Title: {title}\n\n{content[:SUMMARY_MAX_INPUT_CHARS]}`.

Parse `obj["bullets"]` → strings; fallback to line-parsing; normalize to ≤ 5 (≥ 3 required).

## 5. Flow

```
articles (summary = NULL)  ← Phase-1 ingestion
        │
summarizer worker (own poll loop)
   SELECT articles WHERE summary IS NULL  (LIMIT N, newest first)
     per article: prompt(title + truncated content) → GLM-4.5-Flash → 5 bullets
                  UPDATE article.summary = {bullets, model, generated_at}
     429 → stop run (resume next tick) · parse/other error → skip (leave NULL)
        │
Postgres (article.summary)  → future swipe UI · Phase-2.5 digest
```

## 6. Idempotency & cost

`WHERE summary IS NULL` makes every run summarize only the not-yet-done articles → re-running is a
no-op once caught up, and it **backfills** the existing corpus for free. Summary is keyed to the
immutable article (URL PK); no re-summarize on re-ingest. Cost: GLM-4.5-Flash free tier = **$0/day**
within rate limits; if exceeded, switch `SUMMARY_*` env to DeepSeek (~cents/day) — no code change.

## 7. Testing — Definition of Done (hermetic)

1. `ruff check .` = 0; `ruff format --check .` clean.
2. `pytest -m "not integration"` green incl. new unit/`@db` tests; coverage ≥ 80%:
   - **adapter** (`OpenAICompatibleSummarizer`): with a mocked `httpx` transport — parses a JSON
     array into 5 bullets; line-parse fallback works; HTTP 429 → retry then `LLMRateLimitError`;
     malformed/empty → `LLMParseError`; never logs the API key. No live network.
   - **port**: a `FakeSummarizer` honours the `Summarizer` contract.
   - **worker** (`@db`): seed articles (some `summary` NULL, some already set) + a `FakeSummarizer`
     → only-NULL get summarized; JSONB shape `{bullets,model,generated_at}` correct; `BATCH_SIZE`
     respected; idempotent re-run = 0 new; a per-article `LLMParseError` leaves that row NULL and
     does not abort the batch; an `LLMRateLimitError` stops the run leaving remaining NULL.
   - **entry point** smoke: `run_summarize.main` is a coroutine; empty `SUMMARY_API_KEY` no-ops.
3. `@db` tests pass in CI against the pgvector service container.
4. `docker compose config` parses with the `summarizer` service.
5. **Live `@integration`** (manual, needs the user's real key): one real GLM-4.5-Flash call over a
   real article → returns 5 non-empty bullets; asserts shape, skips gracefully on missing key /
   network error (never hard-fails CI — integration is manual only).
6. CI green on the PR; SPEC/architecture/planning updated; memory note added. Never push `main`.

## 8. Seam compliance

New files: `core/llm/{base,openai_compatible,settings,exceptions}.py`,
`worker/{summarize_worker,run_summarize}.py`, an Alembic migration, tests. Additive edits: the
`summary` column on `Article` (nullable, additive), a `summarizer` compose service. Config is a
per-module `SummarizerSettings` fragment — **no edit to core `Settings`**. No edits to `engine.py`,
`core/registry.py`, `repositories.py`, root `cli.py`, or the existing scraper/transform/ingest
workers. `SUMMARY_API_KEY` lives only in `.env` (gitignored); CI never calls the live API.

## 9. Out of scope (later phases)

- **Phase 2.5:** wire summaries into the digest — add a Postgres source to `digest/service.py`
  (it currently reads only `sample`/`jsonl:`), a `bullets` field on `DigestItem`, matcher +
  renderer changes, and the async plumbing the sync digest needs.
- **Phase 3:** AI relevance ranking ("what gets shown").
- **Phase 4:** swipe UI + feedback loop.
- Embeddings / semantic search (the `embedding` column stays NULL).
- Re-summarization on content change; multi-version summaries; per-subscriber custom summaries.
