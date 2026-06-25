# DEPLOYMENT.md — GitHub Actions + Neon runbook (free, no credit card)

> **Target:** the lean Substack pipeline running on **free GitHub Actions scheduled workflows**
> against a free **Neon** serverless Postgres. No Render, no Redis, no MinIO, no always-on server,
> **no credit card**. GitHub Actions provides the free cron compute; Neon stores the data.
>
> The daily flow is two workflows sharing one Neon DB:
> - **`daily-pipeline.yml`** (05:00 UTC) — `init-db → ingest → summarize` (scrape + AI score into Neon).
> - **`daily-digest.yml`** (09:00 Europe/London, DST-safe) — sends the relevance-ranked email.
>
> Render is still supported as a **paid** alternative (`render.yaml`) — see the appendix. The full
> docker-compose stack (`deployment/docker-compose.yml`) remains the scale-out option.

---

## 1. Create a Neon database (free)

1. Sign in at [neon.tech](https://neon.tech) and create a new project (any region; pick one near you).
2. In the **Connection Details** panel, select the **pooled** connection string (Neon's connection
   pooler keeps serverless cold-start latency low).
3. Convert the DSN for asyncpg:
   - Neon gives you: `postgresql://user:pass@host/db?sslmode=require`
   - Change scheme to: `postgresql+asyncpg://user:pass@host/db`
   - Remove the `?sslmode=require` query parameter — SSL is controlled by the separate
     `DATABASE_SSL=require` env var (already set in both workflows), which tells `make_engine` to
     pass `ssl=True` in asyncpg connect-args (asyncpg does not accept the query parameter directly).
4. Save the converted DSN — it becomes the `DATABASE_URL` GitHub secret (next step).

---

## 2. Set the GitHub secrets and variables

In your GitHub repo: **Settings → Secrets and variables → Actions**.

**Secrets** (tab: *Secrets*) — encrypted, never printed in logs:

| Secret | What to put |
|---|---|
| `DATABASE_URL` | the asyncpg DSN from §1 (`postgresql+asyncpg://…`) |
| `SUMMARY_API_KEY` | your z.ai GLM key (free GLM-4.5-Flash) or any OpenAI-compatible key |
| `SUMMARY_PORTFOLIO` | *(optional)* your holdings, CSV — relevance is scored against this |
| `SUMMARY_INTERESTS` | *(optional)* your interests/keywords, CSV |
| `DIGEST_SMTP_USER` | SMTP username / sender address (e.g. your Gmail) |
| `DIGEST_SMTP_PASSWORD` | Gmail **App Password** (Google Account → Security → 2-Step → App passwords) |
| `DIGEST_FROM` | sender address shown in the email |
| `DIGEST_TO` | recipient address for the digest |

**Variables** (tab: *Variables*) — non-secret:

| Variable | Value |
|---|---|
| `DIGEST_SOURCE` | `postgres` — flips the daily email from the bundled sample to the real Neon digest |

> Non-secrets that already have safe inline defaults in the workflows (`DATABASE_SSL=require`,
> `SUMMARY_MODEL=glm-4.5-flash`, `SUMMARY_API_BASE_URL`, and a throwaway `STATE_STORE_KEY`
> placeholder) need **no** action. Override `SUMMARY_MODEL` / `SUMMARY_API_BASE_URL` via repo
> *Variables* only if you switch LLM providers.

> **NEVER** commit a real secret to git. Workflows reference secrets only as `${{ secrets.NAME }}`;
> GitHub injects the values at runtime from its encrypted store.

---

## 3. First run: prove it end-to-end

GitHub schedules don't fire until their time comes, so kick the first run manually:

1. **Run the pipeline.** Repo → **Actions** → **Daily pipeline (ingest + summarize)** → **Run
   workflow**. It runs `init-db` (idempotent: pgvector + tables + `summary`/`relevance` columns),
   then `ingest` (scrapes the 50 Substacks into Neon), then `summarize` (LLM fills `summary` +
   `relevance` for every `summary IS NULL` row). Watch the step logs; expect rows in the Neon
   `articles` table afterward (check in the Neon SQL editor: `SELECT count(*) FROM articles;`).

2. **Send the digest.** Repo → **Actions** → **Daily digest** → **Run workflow**. With
   `DIGEST_SOURCE=postgres` it reads the scored rows from Neon and emails a relevance-ranked digest.
   Confirm a real email lands in `DIGEST_TO`, ordered by relevance with the 5-bullet summaries.
   (The digest sends to the single bundled subscriber `data/subscribers/dee.json`; multi-subscriber
   support is a later phase.)

From then on both run automatically every day.

If a step fails, open its log. Common causes:
- `DATABASE_URL` wrong scheme or missing the `postgresql+asyncpg://` prefix.
- `DATABASE_SSL` not `require` (Neon requires TLS) — it's set inline, so this only bites if edited.
- SMTP creds wrong (Gmail: use an **App Password**, not your account password).
- `summarize` says "skipped (no SUMMARY_API_KEY)" → the `SUMMARY_API_KEY` secret is unset/empty.

---

## 4. Schedule and DST note

| Workflow | Schedule (UTC) | Local time |
|---|---|---|
| `daily-pipeline` | `0 5 * * *` — 05:00 UTC | 05:00 GMT / 06:00 BST |
| `daily-digest` | `0 8` + `0 9 * * *` (gated) | **09:00 Europe/London year-round** |

GitHub cron is always **UTC**. The pipeline runs at a fixed 05:00 UTC — hours of buffer before the
email, enough to absorb GitHub's occasional 10–30 min scheduling drift at peak. The digest workflow
fires at both 08:00 and 09:00 UTC and an internal gate lets exactly one proceed (the one that is
09:00 in `Europe/London`), so the email lands at 09:00 London in both BST and GMT — no manual DST
adjustment needed.

---

## 5. Security reminders

- **Secrets live only in GitHub's encrypted store.** Workflows contain only `${{ secrets.NAME }}`
  references — never a literal value. A `tests/test_daily_pipeline_workflow.py` guard asserts the
  secret env vars are referenced, not inlined.
- **Rotate credentials on exposure.** If a Gmail App Password or GLM key is ever committed or
  logged, revoke it immediately (Google Account → Security → App passwords; z.ai dashboard →
  regenerate) and update the GitHub secret.
- **Neon TLS.** All connections to Neon go over TLS via `DATABASE_SSL=require`. Do not change it.
- **No secrets in logs.** The pipeline CLI does not log DSNs or API keys.
- **SQL injection.** All DB access is SQLAlchemy-parameterized; the only raw SQL is static DDL
  (`CREATE EXTENSION`, `ALTER TABLE … IF NOT EXISTS`) with no external data. A
  `tests/test_no_raw_sql.py` guard asserts no dynamic/f-string SQL exists in `scrapeforge/`.

---

## 6. Per-user digests (Phase 3.5)

### One-time live migration

`create_all` (called by `init-db`) creates tables but does **not** alter existing ones. Run this
once in the Neon SQL editor after deploying Phase 3.5:

```sql
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS email text;
```

No Alembic — this is the correct manual step. The column is nullable; existing rows remain
unaffected and are skipped at send time.

### Hezzian app responsibility

The Hezzian web app (Clerk-authenticated) must write a row to `user_profiles` at user signup:

```sql
INSERT INTO user_profiles (user_id, email, portfolio, sectors, focus, updated_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email, updated_at = now();
```

The pipeline is read-only against `user_profiles`. If `email IS NULL` the user is silently
skipped at send time. If the user has no scored articles (no entries in `user_article_relevance`)
their digest is also skipped.

### Activate the per-user workflow

1. Verify that `DATABASE_URL`, `EMBED_API_KEY`, `DIGEST_SMTP_USER`, `DIGEST_SMTP_PASSWORD`,
   `DIGEST_FROM` secrets are all set (reuse the ones from §2).
2. Open `.github/workflows/daily-digest-users.yml` and uncomment the `schedule:` block when
   ready to run daily.
3. Test first: Repo → **Actions** → **Daily digest (per-user)** → **Run workflow** (manual
   dispatch). Confirm each active user receives their own ranked email.

The owner digest (`daily-digest.yml`) is completely unchanged by Phase 3.5.

---

## 7. User sync (Phase 3.6)

Users are managed by the Hezzian web app in a **separate** Neon database (`hezzian`). The sync
job reads onboarded users from `hezzian` and upserts them into `scraper_news.user_profiles` so
the Phase-3 embedding and per-user digest pipeline can consume them.

### Required secret

Add this to **Settings → Secrets and variables → Actions → Secrets**:

| Secret | What to put |
|---|---|
| `HEZZIAN_DATABASE_URL` | asyncpg DSN for the `hezzian` Neon DB (`postgresql+asyncpg://…`). Same DSN-conversion rules as §1. |

> **Idle-skip:** when `HEZZIAN_DATABASE_URL` is empty (or the secret is not set), the
> `sync-users` step exits cleanly with a notice and the workflow stays green. No error, no
> disruption to downstream steps. Set the secret only when you are ready to sync.

### Required table shape in `hezzian`

The `hezzian` database must expose these tables (Clerk creates them automatically):

```sql
-- users (Clerk manages)
users (clerk_user_id text PRIMARY KEY, email text, deleted_at timestamptz)

-- user_profiles (Hezzian app writes at onboarding)
user_profiles (
    user_id text REFERENCES users(clerk_user_id),
    interests jsonb,           -- {watch_tickers, sectors, asset_classes, investor_type, …}
    onboarding_completed boolean
)
```

The sync job reads these tables with a static SELECT (no writes to `hezzian`). No migration is
needed in `hezzian`; it is consumed read-only.

### How it fits into `daily-pipeline.yml`

The `sync-users` step runs **after `seed-owner` and before `embed-profiles`** so newly synced
profiles are embedded in the same daily run:

```
init-db → ingest → summarize → prune → seed-owner → sync-users → embed-articles → embed-profiles → score-users
```

### First-run verification

After setting `HEZZIAN_DATABASE_URL`, run the pipeline manually (Repo → **Actions** →
**Daily pipeline** → **Run workflow**). Check the `sync-users` step log for:

```
Synced N users from hezzian → scraper_news
```

Then verify in the Neon SQL editor (`scraper_news`):

```sql
SELECT user_id, email, portfolio, sectors FROM user_profiles;
```

---

## Appendix — Render (paid alternative)

`render.yaml` defines the same pipeline as four Render **Cron Jobs**. Render Cron Jobs are a
**paid** feature (they require a card on file), which is why the free GitHub Actions path above is
the default. If you later want Render: **New → Blueprint** → connect the repo (Render detects
`render.yaml` and creates `ingest`, `summarize`, `digest`, `init-db`), then set every `sync: false`
env var (same secret list as §2, plus the `DIGEST_SMTP_HOST`/`DIGEST_SMTP_PORT` and a real
`STATE_STORE_KEY`) in the Render dashboard. Trigger `init-db` manually once, then the three crons
run daily. The non-secret values are already inline in `render.yaml`.
