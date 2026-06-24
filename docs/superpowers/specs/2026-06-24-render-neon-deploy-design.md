# Design — Lean live deployment: Render Cron Jobs + Neon Postgres

- **Date:** 2026-06-24
- **Status:** approved (brainstorming → spec)
- **Branch:** `feat/render-neon-deploy`
- **Goal:** take the existing, merged Substack pipeline **live** — hosted as scheduled run-once
  jobs on **Render**, writing to a managed **Neon** Postgres — and prove the full loop end-to-end
  (real articles → AI summary+score → a real ranked email). Swipe product paused; no new buckets.

## 1. Scope & non-goals

**In scope:** make the *already-built* Substack→summary→relevance→email loop run on real infra.
**Explicitly out:** the multi-user swipe web app, browser drivers / protected buckets (WSO,
premium, YouTube), Redis, MinIO, always-on workers, the Caddy/API services.

**Two hard requirements (owner):**
- **No exposed API keys** — every secret lives only in Render's encrypted env; nothing in git.
- **SQL-injection-safe** — all DB access stays SQLAlchemy-parameterized; audited.

## 2. Runtime shape (lean)

The pipeline runs as **scheduled run-once jobs** on Render, each a single `scrapeforge …` command
that connects to Neon, does its batch, and exits. **No Redis, no MinIO, no always-on processes.**

```
Neon Postgres (managed, pgvector)  ◄── the single source of truth
        ▲              ▲              ▲
   ┌────┘         ┌────┘         ┌────┘
 ingest        summarize       digest send       Render Cron Jobs (run → exit)
 ~06:00 UTC     ~06:30 UTC      ~08:00 UTC
 scrape 50 →    score rows      send the real
 PostgresSink   WHERE summary   relevance email
                IS NULL         (Gmail SMTP, --source postgres)

 init-db  — one-off (manual first run): CREATE EXTENSION vector + create_all + ensure_summary_columns
```

## 3. New code (this repo) — `pipeline` CLI sub-app

A new per-feature Typer sub-app `scrapeforge/pipeline/cli.py`, mounted once in the root `cli.py`
(the sanctioned `add_typer` seam), exposing three **run-once** commands. Render cron commands are
`scrapeforge pipeline <cmd>`.

### 3.1 `pipeline init-db`
- **Responsibility:** make a fresh Neon database ready, idempotently.
- **Does:** open an engine on `Settings().DATABASE_URL`; `CREATE EXTENSION IF NOT EXISTS vector`
  (Neon supports pgvector — required by the `embedding Vector(1536)` column); `Base.metadata.
  create_all`; `ensure_summary_columns(engine)` (the existing idempotent migration). All idempotent
  → safe to re-run. Run **once** on first deploy (Render manual job trigger).

### 3.2 `pipeline ingest`
- **Responsibility:** scrape the 50 curated Substacks straight into Postgres — the lean path that
  needs neither Redis nor MinIO.
- **Does:** for each `substack_sources.select_sources()`, `await SubstackScraper.scrape_publication(
  base, limit)`; write each `status=="success"` result via the existing **`PostgresSink`** (UPSERT,
  idempotent on `sha256(url)`), skipping `sink.seen(url)` within the run. **No queue, no object
  store** (raw-archival dropped — the cleaned content lives in Postgres). Reuses the tested
  `scrape_publication` + `PostgresSink` — this is the Phase-1 ingest logic minus the queue/MinIO.
- **Flags:** `--limit` (posts/publication, default 25), `--sector`/`--max` (reuse the selection
  helpers) for ad-hoc runs.

### 3.3 `pipeline summarize`
- **Responsibility:** run the summarizer once and exit.
- **Does:** build `SummarizerSettings` + `OpenAICompatibleSummarizer`; call the existing
  `run_summarize_worker(...)` which **drains `WHERE summary IS NULL` in batches until empty**, then
  return. Idle/no-op when `SUMMARY_API_KEY` is empty (clear log, exit 0). Reuses the Phase-2 worker.

### 3.4 Digest send (no new code)
The daily email is the existing `scrapeforge digest send --yes --source postgres` (Phase 2.5),
scheduled as the third cron with `DIGEST_SOURCE`/SMTP env.

## 4. Neon connectivity (SSL)

Neon requires TLS. asyncpg + SQLAlchemy needs SSL enabled — handled at the engine layer:
`make_engine` gains an **opt-in SSL path** (e.g. honor a `DATABASE_SSL=require` env / a `?ssl=require`
DSN param) that adds `connect_args={"ssl": ...}` **only when set**, so local/container tests (no SSL)
are unaffected. The exact mechanism (URL param vs `connect_args`) is **verified against Neon during
the live run** and pinned then. Neon's **pooled** connection string is used (run-once jobs each open
a fresh short-lived connection; the pooler absorbs cold starts).

## 5. Render configuration

- **`render.yaml`** (Render Blueprint) defines the jobs:
  - `type: cron` services for `ingest`, `summarize`, `digest` (+ a manually-triggered `init-db`
    job), each `runtime: docker`, `dockerfilePath: deployment/Dockerfile.api`,
    `dockerCommand: scrapeforge pipeline <cmd>` (digest: `scrapeforge digest send --yes --source postgres`).
  - `schedule:` cron expressions in **UTC** (ingest 06:00, summarize 06:30, digest 08:00 — note a
    DST caveat for the email hour, documented).
  - `envVars:` list the names with **`sync: false`** (set in the Render dashboard, never in the
    file): `DATABASE_URL`, `DATABASE_SSL`, `STATE_STORE_KEY`, `SUMMARY_API_KEY`, `SUMMARY_MODEL`,
    `SUMMARY_API_BASE_URL`, `SUMMARY_PORTFOLIO`, `SUMMARY_INTERESTS`, `DIGEST_SMTP_HOST/PORT/USER/
    PASSWORD`, `DIGEST_FROM`, `DIGEST_TO`, `DIGEST_SOURCE=postgres`. **No secret values in `render.yaml`.**
- **Dockerfile:** reuse `deployment/Dockerfile.api` (slim `python:3.12-slim`, `pip install .`, non-root)
  — no browser deps needed for the Substack/curl_cffi path.

## 6. Security (the two requirements)

- **Secret exposure:** `render.yaml` carries only env-var *names* (`sync: false`); `.env` is
  gitignored; the summarizer never logs its key; the CI **secret-scan** job stays on. A plan step
  greps the staged diff for anything resembling a key/DSN before each commit, and `DEPLOYMENT.md`
  tells the owner to paste secrets **only** into the Render dashboard.
- **SQL injection:** a plan **audit step** confirms every query is SQLAlchemy ORM/Core with bound
  parameters — no f-string/`%`/`.format` SQL with external input. The only raw SQL is **static DDL**
  (`CREATE EXTENSION`, `ALTER TABLE … IF NOT EXISTS`) with no external data. New `ingest`/`init-db`
  code uses ORM writes + static DDL only.

## 7. Testing — Definition of Done

**Hermetic (CI / local, what I build):**
1. `ruff check .` = 0; `ruff format --check .` clean.
2. `pytest -m "not integration"` green incl. new unit/`@db` tests; coverage ≥ 80%:
   - **`pipeline ingest`** (`@db`): with a fake scraper → writes the success articles to Postgres,
     idempotent re-run (no dup rows), skips paywalled/non-success, no Redis/MinIO touched.
   - **`pipeline summarize`** (`@db`): with a fake summarizer → drains `summary IS NULL`, writes
     `relevance`+`summary`; empty-key → no-op.
   - **`pipeline init-db`** (`@db`): idempotent (run twice, no error; columns/extension present).
   - **`make_engine` SSL path**: with the SSL env set, `connect_args` carries ssl; unset → no ssl
     (local unaffected). (Unit test of the wiring; no live connect.)
   - **`render.yaml`**: valid YAML; every `envVars` entry is `sync: false` (no inline secret).
   - **SQLi audit**: a test/grep asserting no f-string/`%`-formatted SQL in `scrapeforge/`.
3. `@db` tests pass in CI against the pgvector service container.
4. CI green on the PR; `DEPLOYMENT.md` + SPEC/architecture/planning updated; memory note added.
   Never push `main`.

**Live (manual, owner-provisioned — the real proof, done together):**
5. Owner creates a Render account + a Neon database, sets the secrets in Render. Then we trigger,
   in order, `init-db → ingest → summarize → digest send` and confirm:
   - real Substack articles in Neon (row count > 0),
   - `relevance`/`summary` populated,
   - a **real relevance-ranked email arrives** in `DIGEST_TO`.
   Then the three crons run it daily. This step is documented step-by-step in `DEPLOYMENT.md` and is
   NOT automated (it needs real accounts + secrets).

## 8. The build-split (who does what)

- **I build (here):** the `pipeline` CLI (init-db/ingest/summarize), the Neon SSL wiring, `render.yaml`,
  the `DEPLOYMENT.md` runbook, the security audits — TDD + reviewer loop.
- **Owner provisions:** Render account + Neon DB; pastes secrets (real GLM key, Gmail app password,
  `DIGEST_TO`, portfolio/interests) into the Render dashboard. (I can't — needs accounts/credentials.)
- **Together:** trigger the first live run, watch the email land.

## 9. Seam compliance

New files: `scrapeforge/pipeline/{__init__,cli}.py`, `render.yaml`, tests. Additive edits: one
`add_typer` line in root `cli.py` (the sanctioned sub-app mount), an opt-in SSL path in
`core/db/session.make_engine`, `DEPLOYMENT.md`, and the docs. Reuses `SubstackScraper.
scrape_publication`, `PostgresSink`, the summarizer, `ensure_summary_columns`, `Dockerfile.api`.
No edits to `engine.py`, `registry.py`, `repositories.py`, `exceptions.py`, the existing
scraper/transform/ingest/summarize workers, or the queue/objectstore code (left in place, unused
by this lean deployment).

## 10. Out of scope (later)

Swipe web app (paused); browser drivers + protected buckets; multi-user/per-user relevance;
embeddings; migrating the always-on/queue deployment (the docker-compose stack stays as an
alternative for a future scale-out).
