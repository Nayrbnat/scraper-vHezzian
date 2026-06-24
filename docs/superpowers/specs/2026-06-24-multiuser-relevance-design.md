# Design — Phase 3: Multi-user per-user relevance (pipeline + data model)

- **Date:** 2026-06-24
- **Status:** draft (brainstorming → spec); awaiting owner review
- **Builds on:** the live single-user pipeline (RSS ingest → shared AI summary + relevance → digest).

## 1. Goal & decisions

Let many users each get a feed of the **same** scraped articles, ranked **per user** by their own
portfolio + sectors — without re-running the LLM per user. Owner decisions (locked):

| Decision | Choice |
|---|---|
| Build scope this phase | **Pipeline + data model only.** No web UI, no auth in this repo. |
| Per-user scoring | **Embeddings + vector similarity** (pgvector). The 5-bullet summary stays shared (one LLM call/article); the per-user score is cheap vector math. |
| Accounts / profiles | **The Hezzian Next.js app owns** users, auth, and profiles, writing them into the **shared** Postgres. This pipeline only **reads** profiles and **writes** per-user scores. |

**Non-goals (later phases):** login/feed UI, swipe feedback loop, per-user email delivery, auth.

## 2. Architecture

```
Hezzian app (Next.js + NextAuth)         ── owns ──►  user_profiles  (writes: portfolio, sectors)
   builds each user's feed  ◄── reads ──             user_article_relevance
                                   │  shared Neon Postgres
                                   ▼
Python pipeline (this repo, daily)
   embed-articles   : Embedder → articles.embedding        (shared, 1 call/article, WHERE NULL)
   embed-profiles   : Embedder → user_profile_vectors      (1 call/user, only when profile changed)
   score-users      : pgvector cosine → user_article_relevance  (in-DB math, no LLM, no API)
```

The pipeline never touches auth. The app and the pipeline meet only at three tables (the **contract**, §3).

## 3. Data model (shared Postgres)

**App-owned (this repo READS via a mapped model; the app's migrations are the source of truth):**

```
user_profiles
  user_id     text  PK          -- matches the app's user id
  portfolio   text[]            -- tickers / company names
  sectors     text[]            -- e.g. {AI, semiconductors, fintech}
  focus       text  NULL        -- optional free-text emphasis (defaults to the global SUMMARY_FOCUS)
  updated_at  timestamptz
```

**Pipeline-owned (this repo writes; additive migration here):**

```
articles.embedding   vector(1536)   -- ALREADY EXISTS on the model; filled by embed-articles

user_profile_vectors
  user_id      text  PK
  embedding    vector(1536)
  source_hash  text              -- sha256(portfolio+sectors+focus); re-embed only on change
  updated_at   timestamptz

user_article_relevance
  user_id      text
  article_id   text  -> articles.id
  score        double precision   -- cosine similarity in [−1, 1], higher = better fit
  computed_at  timestamptz
  PRIMARY KEY (user_id, article_id)
  INDEX (user_id, score DESC)      -- the app's "top feed for user X" query
```

`init-db` is extended to create the pipeline-owned tables idempotently and to **ensure
`user_profiles` exists** (so the pipeline is testable standalone; in production the app's migration
owns it — `CREATE TABLE IF NOT EXISTS` is a no-op when the app already made it).

## 4. New components (each: responsibility / interface / deps)

### 4.1 `core/embeddings/` — the Embedder port (mirrors the Summarizer port)
- `base.py`: `Embedder` ABC — `async def embed(self, texts: list[str]) -> list[list[float]]`.
- `gemini.py`: `GeminiEmbedder` (PRIMARY) — calls Gemini `gemini-embedding-001` with
  `output_dimensionality=EMBED_DIM` (1536); batches; never logs the key; 429/timeout handling mirrors
  the summarizer adapter.
- `openai_compatible.py`: `OpenAICompatibleEmbedder` (fallback, e.g. Jina) — POSTs to `{BASE}/embeddings`.
- `settings.py`: `EmbedderSettings` fragment — `EMBED_PROVIDER` (`gemini` | `openai_compatible`),
  `EMBED_API_KEY`, `EMBED_API_BASE_URL`, `EMBED_MODEL`, `EMBED_DIM` (default 1536), `EMBED_BATCH_SIZE`.
  A small factory picks the adapter by `EMBED_PROVIDER`. **`EMBED_DIM` must match the `vector(N)` columns.**

### 4.2 `pipeline/embeddings_jobs.py` — the three jobs (pure-async, injected adapters)
- `embed_articles(*, session_factory, embedder, batch_size) -> int`: select `WHERE embedding IS NULL`
  (recent first), embed `title + content[:N]`, write `articles.embedding`. Idempotent.
- `embed_profiles(*, session_factory, embedder) -> int`: read `user_profiles`; for each whose
  `sha256(portfolio+sectors+focus)` ≠ stored `source_hash`, embed the profile text and upsert
  `user_profile_vectors`. Skips unchanged users (no wasted calls).
- `score_users(*, session_factory, window_days, top_k) -> int`: for each user with a profile vector,
  `SELECT article_id, 1 - (a.embedding <=> :uvec) AS score FROM articles a WHERE embedding IS NOT NULL
  AND fetched_at >= now()-window ORDER BY a.embedding <=> :uvec LIMIT top_k`, then UPSERT into
  `user_article_relevance`. Pure pgvector; no LLM, no external API. Bound params only (SQLi-safe).

### 4.3 `pipeline/cli.py` — three new run-once subcommands
`pipeline embed-articles`, `pipeline embed-profiles`, `pipeline score-users` (each: selector loop +
`asyncio.run` + engine dispose, like the existing jobs). Idle-skip when `EMBED_API_KEY` is empty.

### 4.4 Owner bootstrap (single-user today)
A tiny `pipeline seed-owner` (or a documented SQL snippet) inserts one `user_profiles` row
(`user_id='owner'`) from `SUMMARY_PORTFOLIO`/`SUMMARY_INTERESTS`/`SUMMARY_FOCUS`, so the whole
per-user path runs for the owner immediately and is identical to the multi-user path.

## 5. Daily workflow

Extend `daily-pipeline.yml` after `summarize`, before `prune`:
`embed-articles → embed-profiles → score-users`. Add `EMBED_*` secrets/vars alongside the `SUMMARY_*`
ones. `prune` also deletes the dependent `user_article_relevance` rows (FK `ON DELETE CASCADE`).

## 6. Scoring detail
v1 = **pure cosine similarity** between the user-profile vector and each article vector. The shared AI
relevance score (1–10) is available as an optional light prior later
(`final = w·similarity + (1−w)·norm(relevance)`) — left as a tunable knob, not built in v1.

## 7. Embeddings provider — DECIDED: free API (Gemini, Jina fallback)
Owner chose a **free embeddings API** ($0). Research (2026) + a probe (z.ai's GLM gateway exposes no
embedding models — error 1211) settled it on:

- **Primary: Google Gemini `gemini-embedding-001`** via a free Google AI Studio key (no credit card).
  Recurring free quota (~1,500 requests/day, 10M tokens/min) suits a daily pipeline, and its
  **output dimension is configurable to 1536 — an exact match for the existing `vector(1536)` columns,
  so NO migration is needed.** It is not OpenAI-wire-compatible, so the Embedder port gets a small
  `GeminiEmbedder` adapter (the port exists precisely to absorb this).
- **Fallback: Jina embeddings v4** — OpenAI-wire-compatible (drops into the OpenAI-compatible adapter),
  but its free grant is tighter for ongoing daily use; set `EMBED_DIM` and migrate the columns if used.

`EmbedderSettings` therefore carries `EMBED_PROVIDER` (`gemini` | `openai_compatible`), the key, model,
and `EMBED_DIM` (default 1536). New secret to add at deploy: `EMBED_API_KEY` (a free Google AI Studio
key). Everything else is settled.

## 8. Testing — Definition of Done (hermetic)
1. `ruff` clean; `pytest -m "not integration"` green incl. new `@db` tests; coverage ≥ 80%.
   - `OpenAICompatibleEmbedder` (respx-mocked): batches, parses vectors, 429/timeout handling, key never logged.
   - `embed_articles` (`@db`): fills `embedding` WHERE NULL; idempotent re-run.
   - `embed_profiles` (`@db`): embeds changed profiles, skips unchanged (`source_hash` gate).
   - `score_users` (`@db`): two users with different profiles + several articles → each user's top-K in
     `user_article_relevance` reflects their own similarity order; user isolation; respects `top_k`/window.
   - migration/init creates the three tables idempotently; `prune` cascades to `user_article_relevance`.
2. `@db` tests pass in CI against the pgvector service. SQLi guard stays green (bound params only).
3. SPEC/architecture/planning + memory updated. Never push `main`.

## 9. Seam compliance
New files: `core/embeddings/*`, `pipeline/embeddings_jobs.py`, migration, tests. Additive edits: three
`add` subcommands in `pipeline/cli.py`, `init-db` table creation, `daily-pipeline.yml` steps. No edits
to `engine.py`, `registry.py`, `repositories.py`, the scraper/summarize/digest modules, or core
`Settings`. `articles.embedding` already exists. Invariant #16/#17 respected.
