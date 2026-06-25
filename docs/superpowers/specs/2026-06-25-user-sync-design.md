# User sync (hezzian → scraper_news) — design

**Status:** approved (brainstorm) — ready for implementation plan
**Date:** 2026-06-25
**Phase:** 3.6 (feeds the Phase-3 relevance + Phase-3.5 delivery pipeline with real users)

## Goal

A pipeline job that reads each onboarded user from the **`hezzian`** app database and upserts a
matching row into **`scraper_news.user_profiles`**, so the existing Phase-3/3.5 pipeline
(`embed_profiles → score_users → digest send-all`) ranks and emails real users with **zero changes**.

## Why

Articles live in the `scraper_news` Neon database; users live in a **separate** `hezzian` database
(Clerk auth + onboarding). Postgres cannot join across databases, so the matching must happen where
the articles + pgvector are (`scraper_news`). This job is the bridge: a read-only pull from
`hezzian`, projected into the app-owned `user_profiles` shape the pipeline already consumes.

## Source schema (live, inspected 2026-06-25)

`hezzian.users` (identity): `id uuid`, `clerk_user_id varchar`, `email varchar NOT NULL`,
`full_name`, `deleted_at` (soft delete), …

`hezzian.user_profiles` (onboarding): `user_id uuid → users.id`, `interests jsonb NOT NULL`,
`investor_type`, `experience_level`, `risk_tolerance`, `primary_objective`, `time_horizon`,
`onboarding_completed bool`, `answers jsonb`, …

`interests` shape (real row):
`{"regions": ["US"], "sectors": ["Tech"], "asset_classes": ["Healthcare"], "watch_tickers": ["NVDA"]}`

## Mapping (hezzian → `scraper_news.user_profiles`)

| target column | source | example |
|---|---|---|
| `user_id` (PK, text) | `users.clerk_user_id` | `user_3FdSxb0…` |
| `email` | `users.email` | `h3z…@gmail.com` |
| `portfolio` (text[]) | `interests->watch_tickers` | `["NVDA"]` |
| `sectors` (text[]) | `interests->sectors` + `interests->asset_classes` | `["Tech","Healthcare"]` |
| `focus` (text) | `investor_type; risk_tolerance risk; primary_objective; time_horizon` + regions | `"student; low risk; growth; 1-3y; US"` |
| `updated_at` | `now()` | |

**Source query** (the canonical join):
```sql
SELECT u.clerk_user_id, u.email, p.interests, p.investor_type, p.experience_level,
       p.risk_tolerance, p.primary_objective, p.time_horizon
FROM user_profiles p
JOIN users u ON u.id = p.user_id
WHERE u.deleted_at IS NULL
  AND p.onboarding_completed = true
```
A user without a completed profile (e.g. the 2nd test user) is correctly excluded.

## Architecture

All additive. No change to `embed_profiles`/`score_users`/`digest send-all` — they keep reading
`scraper_news.user_profiles` as-is.

```
daily-pipeline.yml
  └─ pipeline sync-users      (NEW — runs before embed-profiles)
       └─ sync_users(hezzian_factory, scraper_factory):
            rows = fetch_onboarded_users(hezzian_factory)     # read hezzian (users ⨝ user_profiles)
            for row in rows:
              profile = map_to_profile(row)                   # pure mapping (jsonb → portfolio/sectors/focus)
              UPSERT scraper_news.user_profiles ON CONFLICT(user_id)
            return count
  └─ embed-articles → embed-profiles → score-users → (digest send-all)
```

### Components

1. **`UserSyncSettings`** (`scrapeforge/pipeline/sync_settings.py`) — per-module fragment
   (Invariant #16): `HEZZIAN_DATABASE_URL: str = ""`. Empty ⇒ the job idle-skips (so CI / a
   deployment without the secret stays green), mirroring how the embed jobs idle-skip without
   `EMBED_API_KEY`.

2. **`pipeline/user_sync.py`**
   - `HezzianUserRow` — a small frozen dataclass of the columns the query returns.
   - `map_to_profile(row) -> dict` — **pure**: parses `interests` (dict or JSON string), builds
     `portfolio` / `sectors` / `focus`. Heavily unit-tested (this holds the business logic).
   - `async fetch_onboarded_users(hezzian_session_factory) -> list[HezzianUserRow]` — runs the
     static join query above. The `hezzian` tables are **not** our SQLAlchemy models (foreign,
     app-owned), so this uses a static `sqlalchemy.text(...)` SELECT with **no interpolated values**
     (zero injection surface) — documented as the deliberate exception to the "ORM-only" seam rule
     for reading a foreign database.
   - `async sync_users(*, hezzian_session_factory, session_factory) -> int` — read → map → upsert
     into `scraper_news.user_profiles` via `pg_insert(...).on_conflict_do_update` (same pattern as
     `embeddings_jobs.seed_owner`, plus `email`). Returns rows upserted.
   - `run_sync_sync(scraper_url, hezzian_url) -> int` — the async-in-sync bridge for the CLI:
     builds **two** engines (scraper read/write + hezzian read-only), runs `sync_users`, disposes
     both. Converts a `postgresql://` hezzian URL to `postgresql+asyncpg://` defensively.

3. **CLI** `pipeline sync-users` (`scrapeforge/pipeline/cli.py`, added command) — idle-skips with a
   clear message when `HEZZIAN_DATABASE_URL` is empty; otherwise runs `run_sync_sync` and echoes the
   count. Mirrors the `_embedder_or_skip` idle pattern.

4. **`daily-pipeline.yml`** — add a `sync-users` step **before** `embed-profiles`, with
   `HEZZIAN_DATABASE_URL: ${{ secrets.HEZZIAN_DATABASE_URL }}` (and existing `DATABASE_URL` =
   scraper). Idle-skips until the secret is set.

5. **`.env`** — the pipeline core reads `DATABASE_URL` (= scraper_news). Set:
   `DATABASE_URL=postgresql+asyncpg://…/scraper_news` and
   `HEZZIAN_DATABASE_URL=postgresql+asyncpg://…/hezzian`. (Keeps the user's `DATABASE_URL_SCRAPER`
   / `DATABASE_URL_HEZZIAN` as documentation; the code reads the two names above.)

## Error handling

- **Idle-skip** when `HEZZIAN_DATABASE_URL` is empty (no users to sync, exit 0).
- A malformed `interests` (not a dict / missing keys) maps to empty lists, never raises — that user
  syncs with an empty portfolio/sectors rather than failing the batch.
- Connection/read errors on `hezzian` propagate as a typed failure (the whole sync is one
  transaction-less read; a hard DB error should surface, not silently produce zero users).
- The upsert is idempotent (PK `user_id`); re-running the sync is a no-op when nothing changed.

## Testing

- **Unit** (`tests/test_user_sync_mapping.py`): `map_to_profile` — `interests` as dict and as JSON
  string; missing/partial keys → empty lists; `focus` composition; null optional fields.
- **Unit** (`tests/test_user_sync_settings.py`): `UserSyncSettings` default `HEZZIAN_DATABASE_URL=""`
  (hermetic, `_env_file=None`).
- **`@db`** (`tests/test_user_sync_upsert.py`): with a **fake** `fetch_onboarded_users` returning
  two rows, `sync_users` upserts two `scraper_news.user_profiles` rows with the mapped fields;
  re-running updates in place (no duplicate). (Avoids needing two live databases in one test
  container — the `hezzian` read is mocked; the upsert hits the real pgvector `user_profiles`.)
- **CLI** (`tests/test_user_sync_cli.py`): `sync-users` registered; idle-skips with empty
  `HEZZIAN_DATABASE_URL`; runs (job mocked) with it set.
- **Live validation** (manual, like Phase 3): run `pipeline sync-users` against the real `hezzian` +
  `scraper_news` and confirm the 1 onboarded test user (`user_3FdSxb0…`, NVDA/Tech) lands in
  `scraper_news.user_profiles`, then `embed-profiles` + `score-users` rank them.
- **Gates:** ruff clean; `pytest -m "not integration"` green incl. above; coverage ≥ 80%.

## Out of scope

- Two-live-database automated integration test (covered by manual live validation + the mocked-read
  `@db` test).
- Deleting `seed_owner` / the SUMMARY_*-owner profile (it coexists harmlessly as another user_id).
- Real-time sync (this is a batch pull each pipeline run; webhook-driven sync is a future option).
- Writing anything back to `hezzian` (the pull is strictly read-only).

## Docs

`SPEC.md` (the sync job + `HEZZIAN_DATABASE_URL`), `architecture.MD` (module + data-flow),
`planning.MD` (Phase 3.6), `DEPLOYMENT.md` (the `HEZZIAN_DATABASE_URL` secret + step), memory.
