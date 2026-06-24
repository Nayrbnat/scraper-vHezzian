# DEPLOYMENT.md — Render + Neon runbook

> **Target:** three daily Render Cron Jobs (ingest → summarize → digest) running against a Neon
> serverless Postgres. No Redis, no MinIO, no always-on server. The full docker-compose stack
> (`deployment/docker-compose.yml`) remains the scale-out option — this runbook covers the lean live
> path only.

---

## 1. Create a Neon database

1. Sign in at [neon.tech](https://neon.tech) and create a new project (choose the region closest to
   your Render deployment; e.g. `us-east-1`).
2. In the **Connection Details** panel, select the **pooled** connection string (Neon's connection
   pooler keeps serverless cold-start latency low).
3. Convert the DSN for asyncpg:
   - Neon gives you: `postgresql://user:pass@host/db?sslmode=require`
   - Change scheme to: `postgresql+asyncpg://user:pass@host/db`
   - Remove the `?sslmode=require` query parameter — SSL is controlled by the separate
     `DATABASE_SSL=require` env var that tells `make_engine` to pass `ssl=True` in asyncpg
     connect-args (the query parameter is not supported by asyncpg directly).
4. Save the converted DSN as `DATABASE_URL` and note `DATABASE_SSL=require` — you will set both in
   the Render dashboard (see §2).

---

## 2. Deploy via Render Blueprint

1. Push or fork this repo to GitHub (Render connects to GitHub/GitLab).
2. In the [Render dashboard](https://dashboard.render.com), click **New → Blueprint** and connect
   the repo. Render detects `render.yaml` and creates all four cron services automatically
   (`ingest`, `summarize`, `digest`, `init-db`).
3. For every `sync: false` env var in `render.yaml`, set the **real value in the Render dashboard**
   (Environment → Cron service → Environment Variables). The secrets are:

   | Env var | What to put |
   |---|---|
   | `DATABASE_URL` | the asyncpg DSN from §1 |
   | `STATE_STORE_KEY` | a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
   | `SUMMARY_API_KEY` | your GLM / OpenAI-compatible API key |
   | `SUMMARY_PORTFOLIO` | your portfolio description (used for relevance scoring) |
   | `SUMMARY_INTERESTS` | your interests/keywords (used for relevance scoring) |
   | `DIGEST_SMTP_HOST` | SMTP host (e.g. `smtp.gmail.com`) |
   | `DIGEST_SMTP_PORT` | SMTP port (e.g. `587`) |
   | `DIGEST_SMTP_USER` | SMTP username / sender address |
   | `DIGEST_SMTP_PASSWORD` | Gmail app password or SMTP password |
   | `DIGEST_FROM` | sender address shown in the email |
   | `DIGEST_TO` | recipient address for the digest email |

   > **NEVER** put a real secret value in `render.yaml` or commit one to git. `render.yaml` only
   > contains env-var names with `sync: false` — Render pulls the values from its encrypted secret
   > store at runtime.

4. The non-secret values (`DATABASE_SSL=require`, `SUMMARY_MODEL`, `SUMMARY_API_BASE_URL`,
   `DIGEST_SOURCE=postgres`) are already set inline in `render.yaml` and require no dashboard action.

---

## 3. First run: init-db → ingest → summarize → digest

Render cron jobs do **not** auto-run on deploy. Trigger them manually in order:

1. **Trigger `init-db` once** (and only once per fresh database).
   In the Render dashboard → `init-db` cron → **Manual Trigger**. Wait for it to complete
   successfully. This creates the pgvector extension, all tables, and the `summary`/`relevance`
   columns on Neon.

2. **Trigger `ingest`** → watch the logs. Expect rows to appear in the Neon `articles` table.

3. **Trigger `summarize`** → the worker calls the LLM API to fill `summary` and `relevance` for
   each article that has `summary IS NULL`. Confirm rows are updated in Neon.

4. **Trigger `digest`** → sends the relevance-ranked email. Verify that a real email arrives in the
   `DIGEST_TO` inbox with article summaries ordered by relevance score.

If any step fails, check the Render log stream for the cron service. Common causes:
- `DATABASE_URL` is wrong scheme or missing `postgresql+asyncpg://` prefix.
- `DATABASE_SSL` not set to `require` (Neon requires TLS).
- SMTP credentials incorrect (Gmail: use an **App Password**, not your account password).

---

## 4. Schedule and DST note

The three production crons run at:

| Job | Schedule (UTC) | Approximate UK time |
|---|---|---|
| `ingest` | `0 6 * * *` — 06:00 UTC | 06:00 GMT / 07:00 BST |
| `summarize` | `30 6 * * *` — 06:30 UTC | 06:30 GMT / 07:30 BST |
| `digest` | `0 8 * * *` — 08:00 UTC | 08:00 GMT / 09:00 BST |

Render cron schedules are always in **UTC**. During British Summer Time (BST, UTC+1, late
March–late October) the email arrives one hour later in local time than in winter. If you want a
fixed 08:00 London delivery year-round, change the digest schedule to `0 7 * * *` in `render.yaml`
before BST starts and revert to `0 8 * * *` when GMT resumes. This is a manual adjustment —
Render does not do timezone-aware cron.

The `init-db` cron is scheduled for `0 0 31 2 *` (February 31, which never occurs), so it never
runs automatically. Always trigger it manually via the Render dashboard.

---

## 5. Security reminders

- **Secrets live only in Render.** `render.yaml` is committed to git with `sync: false` — no
  secret values, ever. Render's secret store is encrypted at rest.
- **Rotate credentials on exposure.** If a Gmail App Password or GLM API key is ever committed or
  logged, revoke it immediately (Google Account → Security → App passwords; GLM dashboard →
  regenerate key). Update the Render env var with the new value.
- **Neon TLS.** All connections to Neon go over TLS enforced by `DATABASE_SSL=require`. Do not
  disable this (do not set `DATABASE_SSL` to anything other than `require` in production).
- **Render image pulls.** Render builds the Docker image from your repo on each deploy. Keep
  `deployment/Dockerfile.api` lean — do not copy `.env` files or secret material into the image.
- **No secrets in logs.** The pipeline CLI does not log DSNs or API keys. If you add logging,
  ensure secrets are masked before they reach stdout/stderr (Render streams logs to its dashboard).
