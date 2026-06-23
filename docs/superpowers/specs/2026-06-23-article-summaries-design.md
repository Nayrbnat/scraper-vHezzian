# Design — Phase 2: per-article AI 5-bullet summary + relevance score

- **Date:** 2026-06-23
- **Status:** approved (brainstorming → spec)
- **Branch:** `feat/article-summaries`
- **Builds on:** Phase 1 community ingestion (articles land in Postgres with `summary = NULL`).

## 1. Context & goal

Phase 1 lands parsed Substack articles in the `articles` table (`id=sha256(url)`,
`title/content/author/publish_date/bucket/meta JSONB`, `embedding Vector(1536) NULL`).

**Goal:** in **one** cheap LLM call per article, produce both:
1. a **5-bullet "is this worth my time?" summary**, and
2. a **relevance score 1–10** ("relevance to *you*"), synthesized from five named criteria,

stored on the article and ready for the future swipe UI (rank by relevance) and the Phase-2.5
digest. The model is the **free Zhipu GLM-4.5-Flash** by default, behind a provider-agnostic
OpenAI-compatible port so it's a `.env` swap (DeepSeek / Qwen).

**User constraints:** no Claude/Anthropic; Chinese ultra-cheap/free API; "practically free"
(GLM-4.5-Flash free tier = $0/day within rate limits).

## 2. The relevance score (1–10)

The model returns a per-criterion breakdown **and** a holistic overall. The five criteria
(verbatim intent from the owner):

| # | Criterion (`key`) | What scores high |
|---|---|---|
| 1 | `relevance` | Related to AI, finance, the owner's **portfolio companies**, or **secular industry changes**. |
| 2 | `credibility` | Written by a famous/respected Substack or a **leading AI researcher**. |
| 3 | `intensity` | Significance of the event — **fundraising, new investment, collaboration, technological breakthrough** rank high. |
| 4 | `personal` | Matches the owner's **specifically stated interests**, including niche ones (e.g. "hybrid bonding"). |
| 5 | `time` | **Time-sensitivity / urgency** — imminent events (e.g. "SpaceX IPO in 3 days") rank high. |

- **Overall** `relevance` (1–10): the model's holistic weighting of the five for *this* owner.
- **Personalization input:** the owner's portfolio + interests, supplied via config (§3.3) and
  injected into the prompt. The score is therefore "relevance to the owner" — correct while the
  system is single-user; a per-(article, user) table is the multi-user follow-up (§9).

## 3. Decisions (confirmed in brainstorming)

| Decision | Choice |
|---|---|
| Provider (default) | **Zhipu GLM-4.5-Flash** (free), OpenAI-compatible port → swappable by config |
| Trigger | **Batch worker**: `WHERE summary IS NULL`, rate-limit paced, idempotent, backfills |
| Summary storage | New nullable **`summary` JSONB** on `articles`: `{bullets, scores, reason, model, generated_at}` |
| Score storage | New nullable **`relevance` INT** column (1–10, indexed) — the headline, queryable/sortable |
| One call | Bullets + scores + overall + reason returned by a **single** chat-completion request |
| Cadence | Summarizer is **its own background worker** (own poll loop + compose service) |
| Digest integration | **Out of scope** — Phase 2.5 (digest needs a Postgres source + async plumbing) |

## 4. Components (each: responsibility / interface / deps)

### 4.1 `core/llm/base.py` — the `Summarizer` port
```python
@dataclass(frozen=True, slots=True)
class SummaryResult:
    bullets: list[str]            # 3–5 non-empty bullets (target 5)
    relevance: int                # overall 1–10
    scores: dict[str, int]        # {relevance,credibility,intensity,personal,time} each 1–10
    reason: str                   # one-line "why this score"
    model: str

class Summarizer(ABC):
    @abstractmethod
    async def summarize(self, *, title: str, content: str, published: datetime | None,
                        portfolio: list[str], interests: list[str]) -> SummaryResult: ...
```
Pure ABC + dataclass; mockable with a fake in tests.

### 4.2 `core/llm/openai_compatible.py` — `OpenAICompatibleSummarizer(Summarizer)`
- Async `httpx` `POST {base_url}/chat/completions`; sends the system + user messages (§5) asking
  for a **JSON object** (compatible with `response_format={"type":"json_object"}` when supported,
  harmless otherwise): `{"bullets":[...], "scores":{...}, "relevance":n, "reason":"..."}`.
- Parse `json.loads`; **fallback** to lenient extraction if the body has stray prose around the
  JSON (regex the first `{...}`); then validate/normalize (§4.4). Map HTTP 429 / timeouts →
  bounded retry+backoff → `LLMRateLimitError`; other non-2xx → `LLMError`. **Never logs the key.**
- Deps: `httpx` (already a dep), `SummarizerSettings`, the `LLMError` hierarchy.

### 4.3 `core/llm/settings.py` — `SummarizerSettings` (per-module fragment)
Per-module `BaseSettings` (NOT core `Settings`; Invariant #16). Reads the same `.env`:
- `SUMMARY_API_BASE_URL: str = "https://api.z.ai/api/paas/v4"` (z.ai OpenAI-compatible base —
  **verify the exact base/path against current z.ai docs at build time**; config-driven).
- `SUMMARY_API_KEY: str = ""` (secret; `.env` only; empty ⇒ worker no-ops with a clear warning).
- `SUMMARY_MODEL: str = "glm-4.5-flash"`.
- `SUMMARY_PORTFOLIO: str = ""` (CSV → list; e.g. `"Nvidia,TSMC,Anthropic"`) — criterion #1.
- `SUMMARY_INTERESTS: str = ""` (CSV → list; e.g. `"hybrid bonding,advanced packaging,SpaceX IPO"`) — criterion #4.
- `SUMMARY_BATCH_SIZE: int = 20`; `SUMMARY_MAX_INPUT_CHARS: int = 12000`;
  `SUMMARY_REQUEST_TIMEOUT: float = 30.0`; `SUMMARY_INTER_REQUEST_DELAY: float = 1.0`.
- Helpers `portfolio()` / `interests()` parse the CSVs (trim, drop blanks) — mirrors the existing
  `SubstackSettings.custom_domains()` pattern.

### 4.4 `core/llm/exceptions.py` — typed errors
`LLMError(ScrapeForgeError)`; `LLMRateLimitError(LLMError)`; `LLMParseError(LLMError)`.
**Validation/normalize** (in the adapter): bullets → drop empties, keep first 5, require ≥ 3;
each sub-score coerced to int and **clamped to 1–10**; overall `relevance` coerced+clamped 1–10;
`reason` non-empty (fallback `""`). If bullets < 3 or required keys missing → `LLMParseError`.

### 4.5 `core/db/models.py` — new columns (additive)
```python
relevance: Mapped[int | None] = mapped_column(index=True, nullable=True)
"""Overall AI relevance score 1–10 (NULL until scored). Indexed for 'top by relevance'."""
summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
"""{bullets: list[str], scores: dict, reason: str, model: str, generated_at: ISO-8601}. NULL until summarized."""
```
One **Alembic migration** adds both columns. `@db` tests use `create_all`, so the columns appear
automatically in tests; the migration is for production.

### 4.6 `worker/summarize_worker.py` — the batch worker
- `async def summarize_pending(*, session_factory, summarizer, settings) -> int` — returns the
  count summarized this run.
- `async def run_summarize_worker(*, session_factory, summarizer, settings) -> None` — poll loop.
- **Behaviour:** `SELECT * FROM articles WHERE summary IS NULL ORDER BY fetched_at DESC LIMIT
  SUMMARY_BATCH_SIZE` (query inlined here, not in `repositories.py`). For each row:
  `await summarizer.summarize(title=…, content=…[:MAX_INPUT_CHARS], published=row.publish_date,
  portfolio=settings.portfolio(), interests=settings.interests())`; on success
  `UPDATE articles SET relevance = result.relevance, summary = {bullets, scores, reason, model,
  generated_at=now(UTC)} WHERE id = …`; count++.
- **Pacing/errors:** sleep `SUMMARY_INTER_REQUEST_DELAY` between calls; `LLMRateLimitError` →
  **stop the run** (rest stay NULL → next tick); `LLMParseError`/other per-article error → **log
  and skip** (row stays NULL → retried), never abort the batch.
- Deps: `session_factory`, an injected `Summarizer`, `SummarizerSettings`.

### 4.7 `worker/run_summarize.py` + deployment
- Entry mirroring `run_transform.py`: build settings, construct `OpenAICompatibleSummarizer`,
  `make_sessionmaker`, run the poll loop. Empty `SUMMARY_API_KEY` → clear warning + idle (no
  crash-loop). Add a `summarizer` service to `deployment/docker-compose.yml` (Dockerfile.api,
  depends_on postgres, `SUMMARY_*` env). `docker compose config` must parse.

## 5. Prompt (one call → bullets + scores)

**System:** *"You are an equity analyst scoring and summarizing articles for a specific investor.
The investor's portfolio: {portfolio}. The investor's stated interests (including niche topics):
{interests}. Today's date is {today}; the article was published {published}.*

*Produce: (a) exactly 5 short bullets (≤ 25 words each) capturing the core claim/thesis, the
company/ticker or sector, the key number or catalyst, the bull/bear angle, and why it matters;
(b) integer 1–10 sub-scores for: `relevance` (about AI, finance, the portfolio, or secular
industry shifts), `credibility` (famous/respected Substack or leading researcher), `intensity`
(fundraising / new investment / collaboration / technological breakthrough rank high), `personal`
(matches the investor's stated interests, niche ones count), `time` (imminent/time-sensitive
events rank high); (c) an overall `relevance` 1–10 weighing those for THIS investor; (d) a
one-line `reason`. Return ONLY a JSON object: {"bullets":[5 strings], "scores":{"relevance":n,
"credibility":n,"intensity":n,"personal":n,"time":n}, "relevance":n, "reason":"…"}."*

**User:** `Title: {title}\n\n{content[:SUMMARY_MAX_INPUT_CHARS]}`.

## 6. Flow

```
articles (summary = NULL)  ← Phase-1 ingestion
        │
summarizer worker (own poll loop)
   SELECT articles WHERE summary IS NULL  (LIMIT N, newest first)
     per article: prompt(title+content, profile, dates) → GLM-4.5-Flash
                  → {bullets[5], scores{5}, relevance, reason}  (one call)
                  UPDATE article.relevance = n,
                         article.summary   = {bullets, scores, reason, model, generated_at}
     429 → stop run (resume next tick) · parse/other error → skip (leave NULL)
        │
Postgres (article.relevance, article.summary)  → swipe UI (rank by relevance) · Phase-2.5 digest
```

## 7. Idempotency & cost

`WHERE summary IS NULL` ⇒ each run scores only the not-yet-done articles; re-running is a no-op
once caught up and **backfills** the existing corpus. Score+summary keyed to the immutable
article (URL PK). Storing the sub-score breakdown means the overall can be **re-tuned later with
our own weights without re-calling the model**. Cost: GLM-4.5-Flash free = **$0/day** within rate
limits; exceed it → switch `SUMMARY_*` env to DeepSeek (~cents/day), no code change.

## 8. Testing — Definition of Done (hermetic)

1. `ruff check .` = 0; `ruff format --check .` clean.
2. `pytest -m "not integration"` green incl. new unit/`@db` tests; coverage ≥ 80%:
   - **adapter:** mocked `httpx` → parses the JSON object into `bullets` + `scores` + `relevance`
     + `reason`; sub-scores/overall **clamped to 1–10**; bullets normalized (≥ 3 else
     `LLMParseError`); lenient-extraction fallback works; HTTP 429 → retry → `LLMRateLimitError`;
     malformed/missing keys → `LLMParseError`; **API key never logged**. No live network.
   - **settings:** `portfolio()`/`interests()` parse CSV (trim/blank-drop), empty ⇒ `[]`.
   - **worker (`@db`):** seed articles (some `summary` NULL, some set) + a `FakeSummarizer` →
     only-NULL get processed; **both** `relevance` (int 1–10) and `summary` JSONB
     (`{bullets,scores,reason,model,generated_at}`) written; `BATCH_SIZE` respected; idempotent
     re-run = 0 new; per-article `LLMParseError` leaves that row NULL without aborting the batch;
     `LLMRateLimitError` stops the run leaving the rest NULL. Asserts the fake received the
     `portfolio`/`interests`/`published` it was given.
   - **entry-point smoke:** `run_summarize.main` is a coroutine; empty `SUMMARY_API_KEY` no-ops.
3. `@db` tests pass in CI against the pgvector service container.
4. `docker compose config` parses with the `summarizer` service.
5. **Live `@integration`** (manual, needs the owner's real key): one real GLM-4.5-Flash call on a
   real article with a sample portfolio/interests → returns 3–5 non-empty bullets + a 1–10
   relevance + 5 sub-scores; skips gracefully on missing key / network error (never fails CI).
6. CI green on the PR; SPEC/architecture/planning updated; memory note added. Never push `main`.

## 9. Seam compliance

New files: `core/llm/{base,openai_compatible,settings,exceptions}.py`,
`worker/{summarize_worker,run_summarize}.py`, an Alembic migration, tests. Additive edits: the
`relevance` + `summary` columns on `Article` (nullable, additive), a `summarizer` compose service.
Config is a per-module `SummarizerSettings` fragment — **no edit to core `Settings`**. No edits to
`engine.py`, `core/registry.py`, `repositories.py`, root `cli.py`, or the existing
scraper/transform/ingest workers. `SUMMARY_API_KEY` lives only in `.env` (gitignored); CI never
calls the live API.

## 10. Out of scope (later phases)

- **Phase 2.5:** wire summaries + relevance into the digest — a Postgres source for
  `digest/service.py` (today it reads only `sample`/`jsonl:`), render the bullets, sort/threshold
  by `relevance`, and unify the owner profile (portfolio/interests) across digest + scorer.
- **Phase 3 → Phase 4:** the swipe UI + feedback loop (swipes re-tune the weighting / fine the
  relevance signal). Per-(article, user) relevance for multi-user.
- Embeddings / semantic relevance (the `embedding` column stays NULL).
- Re-summarization on content change; multi-version summaries.
