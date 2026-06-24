# Design — Phase 2.5: relevance-ranked digest with AI bullets

- **Date:** 2026-06-24
- **Status:** approved (brainstorming → spec)
- **Branch:** `feat/digest-relevance`
- **Builds on:** Phase 2 (articles in Postgres now carry `summary` JSONB + `relevance` INT).

## 1. Goal

Turn the daily Hezzian email into a single **"Top updates" list ranked by the AI relevance
score**, pulling real scored articles from Postgres. Each item shows its **5 bullets**, a
**relevance badge**, and the one-line **reason**. The existing keyword-matching path
(`sample`/`jsonl:` sources) stays intact and unchanged. No architecture change.

## 2. Confirmed decisions (brainstorming)

| Decision | Choice |
|---|---|
| Ranking model | **Relevance-first single ranked list** (drops keyword sections — relevance already encodes the owner's portfolio/interests) |
| Inclusion | **Top N above a floor**: top `DIGEST_TOP_N=10`, only items `relevance >= DIGEST_RELEVANCE_FLOOR=5` (both configurable) |
| Window | **Last `DIGEST_WINDOW_HOURS=48`h** (configurable) |
| Item render | 5 bullets + relevance badge (`9/10`) + `why: <reason>` |
| Source | New `--source postgres`; daily workflow switches to it |
| Empty day | Send a short **"no high-relevance updates today"** email (do not skip the send) |

## 3. Flow

```
digest preview/send --source postgres
  └─ load_ranked_articles_sync(window=48h, limit=10)        # async read, asyncio.run bridge
       SELECT * FROM articles
       WHERE summary IS NOT NULL AND fetched_at >= now-48h
       ORDER BY relevance DESC NULLS LAST, fetched_at DESC
  └─ build_relevance_digest(subscriber, articles, min_relevance=5, limit=10, now)
       filter relevance>=floor → sort relevance desc → cap top-N → map → one "Top updates" section
  └─ render_email()  (badge + bullets + reason; lead-text fallback for sample/jsonl)
  └─ PreviewEmailSender / SmtpEmailSender   (unchanged)
```

## 4. Components (each: responsibility / interface / deps)

### 4.1 `digest/models.py` — additive fields
- `DigestItem` (Pydantic, `extra="forbid"`) gains:
  - `bullets: list[str] = Field(default_factory=list)` — the AI 5 bullets (empty for keyword path).
  - `relevance: int | None = None` — 1–10 score (None for keyword path).
  - `reason: str | None = None` — one-line "why".
- `DigestSection.key` Literal gains `"top"` (the relevance-ranked section).
- Deps: none. Existing keyword path keeps working (new fields default to empty/None).

### 4.2 `digest/relevance.py` (new) — `build_relevance_digest`
- **Responsibility:** assemble a relevance-ranked `Digest` from a list of `Article`s. Pure, no DB.
- **Interface:**
  ```python
  def build_relevance_digest(
      subscriber: Subscriber,
      articles: list[Article],
      *,
      min_relevance: int = 5,
      limit: int = 10,
      now: datetime | None = None,
  ) -> Digest: ...
  ```
- **Behaviour:** read each article's `relevance` + `summary.bullets`/`summary.reason` from
  `article.metadata` (the Postgres source stashes them there — §4.3); keep `relevance >=
  min_relevance`; sort by `relevance` desc, then `publish_date`/`fetched_at` desc; take the first
  `limit`; map each to a `DigestItem(title, url, source, published, summary=<lead-text fallback>,
  bullets, relevance, reason)`; wrap in a single `DigestSection(key="top", heading="Top updates")`.
  Empty result → a `Digest` with no sections (renders the empty-state copy).
- **Deps:** `digest.models`, `core.models.Article`, the existing `summarize()`/`_to_item` helpers
  in `matcher.py` for the lead-text fallback + source-domain extraction (reuse, don't duplicate).

### 4.3 `digest/postgres_source.py` (new) — load ranked articles
- **Responsibility:** read recent summarized articles from Postgres, newest-relevance-first, and
  map them to `core.models.Article` DTOs carrying bullets/relevance/reason in `metadata`.
- **Interface:**
  - `async def load_ranked_articles(session_factory, *, window_hours: int, limit: int) -> list[Article]`
    — `SELECT … WHERE summary IS NOT NULL AND fetched_at >= now(UTC)-window ORDER BY relevance
    DESC NULLS LAST, fetched_at DESC LIMIT limit`, inlined here (NOT in `repositories.py`). Rows
    are relevance-desc, so the top `limit` are the highest-scoring; the builder's floor only trims
    from the bottom, so no over-fetch is needed. Maps each `ArticleRow` →
    `Article(url, title, content, author, publish_date, metadata={source_domain, bucket,
    relevance, summary})` where `metadata["relevance"]=row.relevance` and
    `metadata["summary"]=row.summary`.
  - `def load_ranked_articles_sync(database_url, *, window_hours, limit) -> list[Article]` — sets
    the Windows selector event-loop policy, builds an engine via `make_engine`, runs the async
    loader with `asyncio.run`, disposes the engine. The async-in-sync bridge at the run-once CLI
    boundary.
- **Deps:** `core.db.session.make_engine/make_sessionmaker`, `core.db.models.Article`, `core.models`.

### 4.4 `digest/settings.py` (new) — `DigestSettings` fragment
- Per-module `BaseSettings` (NOT core `Settings`): `DIGEST_RELEVANCE_FLOOR: int = 5`,
  `DIGEST_TOP_N: int = 10`, `DIGEST_WINDOW_HOURS: int = 48`. `model_config = SettingsConfigDict(
  env_file=".env", extra="ignore")`. (The existing `DIGEST_SMTP_*`/`DIGEST_TO` are read elsewhere;
  this fragment only adds the ranking knobs.)

### 4.5 `digest/render.py` (modify) — bullets + badge + reason
- `_html_item` / `render_text`: when `item.bullets` is non-empty, render a relevance **badge**
  (`{relevance}/10`, color scaled: ≥8 green, 5–7 amber, else grey), the **bullets** as a `<ul>`
  (text: `• ` lines), and a muted **`why: {reason}`** line — instead of the lead-text blurb.
  When `bullets` is empty (sample/jsonl), render exactly as today (the `summary` blurb +
  `matched_on` chips). Both HTML and text kept in sync. Inline styles only (email-safe).
- Empty-digest copy generalized to "No high-relevance updates in the last 48 hours."

### 4.6 `digest/service.py` + `digest/cli.py` (modify)
- `get_articles(source)` gains a `postgres` branch → `load_ranked_articles_sync(Settings().DATABASE_URL,
  window_hours=DigestSettings().DIGEST_WINDOW_HOURS, limit=DigestSettings().DIGEST_TOP_N)`.
- A `make_relevance_digest(subscriber, articles, settings)` / branch in `make_digest`: when the
  source is `postgres`, build via `build_relevance_digest(... min_relevance=floor, limit=top_n)`;
  otherwise the existing `build_digest` (keyword). `deliver(...)` unchanged otherwise.
- `cli.py`: `--source` already exists; document `postgres`. Optional CLI overrides
  `--floor`/`--top`/`--window` (default from `DigestSettings`). The **daily workflow**
  (`.github/workflows/…digest…`) switches its `--source` to `postgres`.

## 5. Async-in-sync

The digest CLI is synchronous and run-once (not inside an event loop), so `load_ranked_articles_sync`
uses `asyncio.run`. On Windows it sets `WindowsSelectorEventLoopPolicy` first (asyncpg safety),
mirroring the community CLI. The async loader owns the engine lifecycle (dispose in `finally`).

## 6. Error handling

- Empty window / nothing clears the floor → a non-empty `Digest` with zero sections → the
  rendered email shows the empty-state copy (still sent — confirmed).
- Missing `DATABASE_URL` → `make_engine` uses the Settings default; a real connection failure
  surfaces as a clear CLI error (caught at the CLI boundary, exit 1), never a stack-trace dump.
- Articles with `relevance IS NULL` (unsummarized) are excluded by the `summary IS NOT NULL`
  filter. Typed exceptions; no bare excepts; async I/O awaited.

## 7. Testing — Definition of Done (hermetic)

1. `ruff check .` = 0; `ruff format --check .` clean.
2. `pytest -m "not integration"` green incl. new unit/`@db` tests; coverage ≥ 80%:
   - **`build_relevance_digest`** (pure, no DB): floor filters out `<min`; sorts relevance desc
     (recency tiebreak); caps at `limit`; maps bullets/relevance/reason; empty input → empty
     digest; lead-text fallback set on `summary`.
   - **`DigestSettings`**: defaults + env override.
   - **render**: an item with bullets → HTML contains the badge, all 5 bullets, and the reason,
     and NOT the lead-text blurb; an item without bullets → renders the legacy blurb + chips
     (no regression); text variant matches.
   - **`postgres_source`** (`@db`): seed articles — summarized in-window (varied relevance),
     summarized out-of-window, unsummarized in-window → `load_ranked_articles` returns only the
     in-window summarized ones, relevance-desc, with bullets/relevance in metadata.
   - **hermetic e2e** (`@db`): seed scored articles → `make_digest(..., source="postgres")` →
     rendered HTML lists items in relevance order, below-floor excluded, badges/bullets present;
     an all-below-floor seed → empty-state email.
3. `@db` tests pass in CI against the pgvector service container.
4. Existing digest tests (keyword path, sample source) still pass unchanged.
5. CI green on the PR; SPEC/architecture/planning + the daily digest workflow updated; memory
   note added. Never push `main`.

## 8. Seam compliance

New files: `digest/{relevance,postgres_source,settings}.py` + tests. Additive edits: `DigestItem`
fields + `DigestSection.key` literal (digest's own model), `render.py`/`service.py`/`cli.py`
(digest's own module), the daily workflow YAML. The Postgres query is **inlined** in
`postgres_source.py` (not added to `repositories.py`). No edits to `engine.py`, `registry.py`,
`repositories.py`, core `Settings`, root `cli.py`, or the scraper/transform/ingest/summarize
workers. The keyword digest path is untouched.

## 9. Out of scope

- Per-subscriber relevance (single-owner for now — the score is owner-relative).
- Changing the keyword-matching path (kept for sample/jsonl).
- The swipe UI (Phase 4) — it consumes the same Postgres relevance/summary via the API.
- Embeddings / semantic ranking.
