# Design — Phase 1 (lean): scheduled community ingestion

- **Date:** 2026-06-23
- **Status:** approved (brainstorming → spec)
- **Branch:** `feat/community-ingestion`
- **Resolves:** the documented community publication fan-out gap (memory
  `community-publication-fanout-gap`).

## 1. Context & problem

The Substack bucket (`SubstackScraper`) and the curated list of 50 investing publications
(`SUBSTACK_INVESTING_SOURCES`) are merged and working. But they only run **on demand**: a human
invokes `scrapeforge community scrape-substacks`, which writes JSONL. Nothing pulls those
articles into the **serving database** on a schedule, so there is no continuously-fresh,
queryable, deduplicated, per-article store to build the planned summary → ranking → swipe
features on.

The Phase-6 ingestion pipeline (scheduler → scraper worker → object store → transform worker →
Postgres) is **single-URL and HTML-CSS-selector based**:

- `worker/scraper_worker.py:handle_scrape_job` calls `engine.scrape(url)` (one URL → one raw
  object); there is no publication fan-out.
- `worker/transform_worker.py` re-extracts fields from raw HTML via `_selectors_for(domain)`.
  `BaseScraper._get_selectors()` returns `{}` and `SubstackScraper` does not override it, so a
  Substack post routed through transform would be marked **error** ("soft block", registered
  `*.substack.com`) or come out **title/author/date-less** (custom domains via the
  `PublicScraper` selector fallback). Substack's rich fields come from the JSON API at *scrape*
  time, not from re-extracting `body_html`.

`SubstackScraper.scrape_publication` already does archive discovery + per-post fetch + full
parse, returning complete `Article`s. So the missing piece is **automation**, not parsing: run
the existing scraper on a cadence and persist its already-complete output to Postgres.

## 2. Goal

Daily, each enabled curated Substack publication pulls its **new** articles into Postgres —
deduplicated, queryable, one row per article — by **reusing** `scrape_publication` and the
existing `PostgresSink`. No discovery worker, no per-post jobs, no transform/envelope changes.

## 3. Architecture & flow

```
scheduler (daily cron)
  └─ for each enabled community Source (the 50; params.platform="substack"):
       create Job (queued)  →  publish IngestMessage → INGEST queue
                                                          │
  community-ingest worker (NEW) ─────────────────────────┘
     • lazy-resolve scraper by platform (substack → SubstackScraper)
     • results = await scraper.scrape_publication(target, limit)   ← EXISTING; complete Articles
     • for each successful Article:
         – skip if PostgresSink.seen(url)        (dedup across runs)
         – archive raw_html → object store        (claim-check preserved)
         – PostgresSink.write(result)             (EXISTING idempotent UPSERT on sha256(url))
     • mark Job done (result_count = # new articles persisted)
     • per-publication failure isolates to that message → retry → DLQ

  Postgres ── digest (unchanged) / future summaries + ranking + swipe
```

Three queues exist after this change: `JOB` (existing single-URL), `RESULTS` (existing), and the
new `INGEST` (community publications). The scheduler routes by `Source.params.platform`.

## 4. Components

Each unit lists **responsibility / interface / dependencies**.

### 4.1 `scrapers/community/substack_sources.py` (+ restore seeding)
- **Responsibility:** turn the curated list into idempotent `Source` rows.
- **Interface:** `async def seed_sources(session, *, limit=25, enabled=True) -> int`. Uses a
  single atomic `postgresql.insert(Source).on_conflict_do_update(index_elements=["name"], ...)`
  — fixes the prior review's intra-batch-dup / concurrent-race concern (no read-then-insert).
  Each row: `name="substack:<host>"`, `bucket="community"`,
  `params={"url": host, "platform": "substack", "limit": limit}`, `cron=None`, `enabled`.
- **Dependencies:** `core/db/models.Source`, SQLAlchemy PG dialect.
- Restore the `community seed-substacks` CLI command (was `scrape-substacks`-adjacent) with
  `--limit/--enabled/--dry-run`. Now correctly wired because an ingestion path exists.

### 4.2 `worker/messages.py` (+ `IngestMessage`)
- **Responsibility:** shared contract for the INGEST queue. Additive; existing `JobMessage` /
  `ResultPointer` untouched.
- **Interface:** `class IngestMessage(TypedDict): job_id: str; platform: str; target: str;
  bucket: str; limit: int`.

### 4.3 `config/settings.py` (+ `INGEST_QUEUE`)
- **Responsibility:** name the new queue alongside `JOB_QUEUE` / `RESULTS_QUEUE`.
- **Interface:** `INGEST_QUEUE: str = "ingest"`. Sanctioned lead edit (consistent with how the
  Phase-6 queues are declared in core `Settings`).

### 4.4 `worker/scheduler.py` (+ routing)
- **Responsibility:** route community-publication sources to ingestion; leave single-URL sources
  on today's path.
- **Interface (change to `enqueue_due_sources`):** for each enabled `Source`, if
  `source.params.get("platform")` is set → create Job + publish `IngestMessage` to
  `settings.INGEST_QUEUE`; else current behaviour (`JobMessage` → `settings.JOB_QUEUE`).
- **Dependencies:** unchanged set (`create_job`, `MessageQueue`, models).

### 4.5 `worker/community_ingest_worker.py` (NEW — the only substantial new logic)
- **Responsibility:** consume one `IngestMessage`, scrape the publication, persist new articles.
- **Interface:**
  - `async def handle_ingest_job(payload: IngestMessage, *, scraper, store, session_factory) -> int`
    — returns the number of new articles persisted. `scraper` is injected (real
    `SubstackScraper` in prod; fake in tests) with an `async scrape_publication(target, limit)`.
  - `async def run_community_ingest_worker(*, queue, store, session_factory, settings) -> None`
    — drain loop over `settings.INGEST_QUEUE` via `queue.consume_once` (mirrors the other
    workers; retry/DLQ handled by the `MessageQueue` port).
  - Platform → scraper resolution is a small lazy-import dispatch (`platform == "substack"` →
    import `SubstackScraper`), mirroring the CLI, so the worker doesn't eagerly import every
    bucket. Reddit slots in later by adding one branch.
- **Behaviour:** mark the Job `running` (started) at entry; open a `PostgresSink(session_factory)`;
  run `scrape_publication`; for each `success` result with an article: `if sink.seen(url):
  continue`; archive raw to `raw_object_key(bucket, url_id(url))` — the article's `raw_html`
  bytes (`text/html`) when present, else a small JSON fallback envelope (`application/json`,
  mirroring the scraper worker) so the raw zone records every persisted post; then
  `await sink.write(result)`. Finally mark the Job `done` with `result_count` (# persisted). A
  raised scrape/publish error marks the Job `error` (with the message) and re-raises so the
  `MessageQueue` retries → DLQ. The ingest worker owns this Job's full lifecycle
  (`queued → running → done | error`) — the transform worker is not involved for community sources.
- **Dependencies:** `PostgresSink`, `ObjectStore`, `url_id`/`raw_object_key`, `update_job_status`,
  the injected scraper.

### 4.6 Entry point + deployment
- `worker/run_community_ingest.py` mirrors `worker/run_scheduler.py` (selector loop on Windows,
  build engine/sink/store/queue from `Settings`, run the drain loop).
- `deployment/docker-compose.yml`: add a `community-ingest` service; `docker compose config`
  must still parse.

## 5. Dedup & idempotency

Two independent guards: `PostgresSink.seen(url)` skips already-ingested posts (no re-fetch / no
re-write), and the UPSERT on `sha256(url)` is the safety net if two runs race. Re-running the
whole chain yields **zero duplicate rows**. Raw archive PUT is deterministic (same key per url).

## 6. Error handling

- Per-publication isolation: a failing `IngestMessage` (publication down, throttle, parse error)
  retries and dead-letters after `QUEUE_MAX_RETRIES`; other publications are unaffected.
- The worker never writes a partial/garbage article: only `status == "success"` results with a
  non-empty parsed article are persisted (the scraper already enforces the paywall/soft-block
  contract, Invariant #15).
- Typed exceptions only; no bare `except`; all I/O `await`-ed (no blocking on the loop).

## 7. Invariant deviation (documented per CLAUDE.md §4)

**Invariant #18 update.** The scraper→transform claim-check split (stateless scraper writes raw +
publishes a pointer; transform is the sole structured writer) governs **public-bucket HTML**.
**Fully-parsing community/JSON scrapers** (Substack, later Reddit) persist structured rows
**within their ingestion worker** via `PostgresSink`, while still archiving raw to the object
store for claim-check/replay. Rationale: these scrapers produce complete `Article`s at fetch
time, so a separate HTML-selector transform stage adds nothing and in fact cannot parse their
JSON-sourced fields. This keeps one normalize path per scraper style rather than forcing a
redundant re-parse. SPEC.md, `architecture.MD`, and `planning.MD` are updated to reflect the
INGEST queue + community-ingest stage.

## 8. Testing — Definition of Done (hermetic, no live infra)

1. `ruff check .` = 0; `ruff format --check .` clean.
2. `pytest -m "not integration"` green incl. new unit/`@db` tests; coverage ≥ 80% (CI gate):
   - **scheduler:** community-platform source → `IngestMessage` on INGEST queue; non-platform
     source → `JobMessage` on JOB queue (existing behaviour preserved).
   - **community-ingest worker:** persists `success` articles; `seen` ones skipped; raw archived
     under the deterministic key; `result_count` correct; non-success / raised scrape → Job
     `error` (message eligible for DLQ); idempotent (second run → no new rows). Fakes for
     queue + object store; ephemeral PG for the sink.
   - **`seed_sources`:** `@db` idempotent upsert (run twice → 50 rows; re-seed updates params);
     uses `ON CONFLICT`, so a duplicate-name batch and a concurrent re-run do not raise.
3. `@db` tests pass in CI against the pgvector service container.
4. `docker compose config` parses with the `community-ingest` service.
5. **Hermetic end-to-end:** seed 1–2 sources → `enqueue_due_sources` → ingest worker (scraper
   mocked to return 2 articles, real object-store fake + ephemeral PG) → `GET /articles` returns
   the community articles **with titles/authors/dates**; re-running the job produces **no
   duplicate** (idempotency); a soft-block/paywalled scrape result yields **no** structured row
   and a Job `error`.
6. CI green on the PR; SPEC/architecture/planning/docs updated; memory updated. Never push main.

## 9. Out of scope (later phases, per the agreed sequencing)

- AI 5-bullet summaries per article (#2).
- AI relevance ranking — "what gets shown" (#3).
- Swipe UI + feedback loop feeding ranking (#4).
- Reddit fan-out (same worker; add one `platform` branch later).
- Cadence is fixed at **daily**; tunable later via cron config.

## 10. Seam compliance

New files (`worker/community_ingest_worker.py`, `worker/run_community_ingest.py`) are additions.
Edits are confined to worker-plane files this feature owns (`scheduler.py`, `messages.py`), the
community bucket's own files (`substack_sources.py`, `cli.py`), the sanctioned shared `Settings`
(queue name, lead edit), deployment, and the spec/architecture docs. No edits to `engine.py`,
`core/registry.py`, `repositories.py`, the root `cli.py`, or the existing scraper/transform
workers. Conventional Commits; PR → `main`; never push `main`.
