# Per-user email delivery — design

**Status:** approved (brainstorm) — ready for implementation plan
**Date:** 2026-06-25
**Phase:** 3.5 (delivery layer on top of Phase 3 per-user relevance)

## Goal

Every user in `user_profiles` receives their own relevance-ranked daily email, built from
**their** per-user cosine scores in `user_article_relevance`. The existing single-owner digest
keeps running unchanged.

## Why now

Phase 3 (merged, PR #19) computes and stores per-user relevance: `score_users` writes a cosine
similarity for every `(user, article)` pair into `user_article_relevance`. Those scores currently
go nowhere — no user receives them. This phase is the **delivery layer**: turn stored scores into
per-user emails. It is the missing "send the most relevant articles to each user" step the owner
described.

## Decisions (locked during brainstorm)

1. **Email source — add an `email` column to `user_profiles`.** The Hezzian app writes each user's
   email (from Clerk) into `user_profiles` at signup, alongside portfolio/sectors. The pipeline
   keeps reading a single app-owned table; no Clerk API call, no new pipeline secret.
2. **Relationship to the owner digest — add per-user as a new, parallel path.** The existing owner
   digest (`digest send`, `daily-digest.yml`, ranked by the global `articles.relevance` column) is
   **not touched**. Per-user delivery is a new command + new workflow. Lowest risk; nothing existing
   breaks.
3. **Score display — rank by the user's cosine score; show the shared 1-10 badge + bullets.** Each
   user's email orders articles by their personal `user_article_relevance.score` (cosine), but each
   card displays the existing shared `articles.relevance` (1-10) + 5-bullet summary + reason. The
   current email template is reused verbatim.

## The three tables (orientation — not all are auth)

| Table | Owner | Holds |
|---|---|---|
| Clerk's auth store | Clerk (managed) | login identity: user id, email, name |
| **`user_profiles`** | app **writes**, pipeline **reads** | user_id, **email (new)**, portfolio, sectors, focus |
| `user_profile_vectors`, `user_article_relevance` | pipeline writes | profile embedding; per-(user,article) cosine score |

`user_profiles` is **our** table (defined in `core/db/models.py`, Phase 3), not Clerk's. The app's
job is to write rows into it; the pipeline reads them.

## Architecture

All work is **additive** — new files and one new column. No edits to the working owner-digest path
(`digest/service.py::deliver`, `digest/relevance.py`, `digest/postgres_source.py`,
`daily-digest.yml`).

```
daily-digest-users.yml  (new cron, 09:00 Europe/London, DST-safe gate — mirrors daily-digest.yml)
  └─ python -m scrapeforge digest send-all --source postgres --yes
       └─ deliver_all(sender, source="postgres"):
            users = load_active_users()            # user_profiles WHERE email IS NOT NULL
            summary = {sent: 0, skipped_empty: 0, failed: 0}
            for user in users:                     # failures isolated per user
              try:
                ranked = load_user_ranked_articles(user.user_id, window, floor, top_n)
                if not ranked:
                    summary.skipped_empty += 1; continue
                digest = build_user_digest(user, ranked)
                sender.send(user.email, render_email(digest))
                summary.sent += 1
              except Exception:                    # log + count, never abort the batch
                summary.failed += 1
            return summary
```

### Components (units)

1. **`UserProfile.email` column** (`core/db/models.py`)
   - `email: Mapped[str | None]` — nullable text. App-owned (the app writes it).
   - Created for fresh DBs by `init-db`'s `create_all(checkfirst=True)`. On the **existing live Neon
     DB** the column is added once by hand: `ALTER TABLE user_profiles ADD COLUMN email text;`
     (there is no Alembic in this project — `create_all` does not alter existing tables). This
     one-time step is documented in the workflow comments and DEPLOYMENT.md.

2. **`digest/user_source.py`** — DB reads for the per-user path (queries inlined here per the seam
   rules, exactly as `digest/postgres_source.py` does for the owner path).
   - `ActiveUser` — a small dataclass/Pydantic value: `user_id`, `email`, `name` (name falls back to
     the email local-part when the app hasn't supplied one; `user_profiles` has no name column in
     v1, so name is derived).
   - `async load_active_users(session_factory) -> list[ActiveUser]` —
     `SELECT user_id, email FROM user_profiles WHERE email IS NOT NULL`.
   - `async load_user_ranked_articles(session_factory, user_id, *, window_hours, score_floor, limit)
     -> list[Article]` — joins scores to articles and returns domain `Article`s carrying their
     shared `relevance` + `summary` in `.metadata` (same metadata shape the owner path uses), in
     cosine-desc order:
     ```sql
     SELECT a.*, uar.score
     FROM user_article_relevance uar
     JOIN articles a ON a.id = uar.article_id
     WHERE uar.user_id = :uid
       AND a.summary IS NOT NULL
       AND a.fetched_at >= now() - :window
       AND uar.score >= :floor
     ORDER BY uar.score DESC, a.fetched_at DESC
     LIMIT :limit
     ```
     Expressed via the SQLAlchemy ORM/Core (no raw SQL string interpolation — SQLi guard, consistent
     with `score_users`). The `uar.score` is read but not displayed (ordering only).
   - `load_all_sync(database_url, *, window_hours, score_floor, limit) -> list[(ActiveUser, list[Article])]`
     — the async-in-sync bridge for the run-once CLI (mirrors `load_ranked_articles_sync`): build
     engine → load users → load each user's ranked articles → dispose. One engine for the whole
     batch.

3. **`digest/user_digest.py`** — `build_user_digest(user: ActiveUser, articles: list[Article], *,
   now=None) -> Digest`. Wraps the already-cosine-ordered `articles` into a single "Top updates"
   `DigestSection`, preserving order (no re-sort — the SQL already ranked them). Each `DigestItem`
   carries the shared 1-10 `relevance`, `bullets`, and `reason` pulled from `article.metadata`. To
   stay DRY, the per-item construction reuses the existing item builder from `digest/relevance.py`
   (factor its private `_item` into a shared, importable `make_item(article)` used by both paths —
   a small, safe refactor of an existing helper, not a rewrite of the owner path's behavior).

4. **`deliver_all(...)`** in `digest/service.py` (new function; existing `deliver` untouched).
   - Signature: `deliver_all(*, source="postgres", sender: EmailSender | None = None) -> DeliverySummary`.
   - Loads users + ranked articles via `user_source`, builds + renders + sends per user, isolates
     failures, returns a `DeliverySummary(sent, skipped_empty, failed)`.
   - Defaults to `PreviewEmailSender` (no creds) when `sender` is None.
   - Only `source="postgres"` is supported (the per-user path is inherently DB-backed); any other
     value raises `ValueError`.

5. **CLI commands** (`digest/cli.py` — adds to the existing sub-app), mirroring the owner
   `preview`/`send` pair:
   - **`digest preview-all`** — `--source` (default `postgres`), `--out-dir` for the HTML.
     Uses `PreviewEmailSender` → writes one HTML per user, sends nothing.
   - **`digest send-all`** — `--source` (default `postgres`), `--yes` (skip the confirm prompt).
     Uses `SmtpEmailSender` (same creds as the owner digest: `DIGEST_SMTP_*`) for real delivery.
   - Both print the `DeliverySummary` (e.g. `sent=12 skipped_empty=3 failed=0`).

6. **`DigestSettings` additions** (`digest/settings.py` — per-module fragment, Invariant #16):
   - `DIGEST_USER_TOP_N: int = 10` — max articles per user email.
   - `DIGEST_USER_WINDOW_HOURS: int = 48` — recency window.
   - `DIGEST_USER_SCORE_FLOOR: float = 0.0` — minimum cosine to include (≥ 0 = positively
     correlated with the user's profile).

7. **`.github/workflows/daily-digest-users.yml`** — new workflow, a copy of `daily-digest.yml`'s
   DST-safe 09:00 Europe/London gate, running `digest send-all --yes --source postgres`. Uses the
   same secrets (`DIGEST_SMTP_*`, `DATABASE_URL`, `DATABASE_SSL=require`, `STATE_STORE_KEY`
   placeholder). Independent of the owner workflow so neither affects the other.

## Error handling

- **Per-user isolation:** each user's build+send is wrapped; a failed render, missing data, or SMTP
  error is logged and counted in `failed`, and the loop continues. One bad user never aborts the
  batch.
- **Empty digest:** a user whose articles all fall below the floor / outside the window yields zero
  items → counted as `skipped_empty`, **no blank email sent**.
- **No users / no email column:** `load_active_users` returns `[]` → summary is all-zero, command
  exits 0 with a clear message (not an error — a brand-new deployment legitimately has no users).
- **Typed exceptions** only inside the loaders (reuse the existing `ScrapeForgeError` hierarchy
  where a typed error fits); the delivery loop catches broadly only at the per-user boundary so one
  user's failure is contained.

## Testing (Definition of Done)

- **`@db`** (`tests/test_user_source.py`): seed two users with different profiles + a shared article
  set + `user_article_relevance` rows → `load_user_ranked_articles` returns each user's articles in
  cosine-desc order, respects `score_floor`, `window_hours`, and `limit`; `load_active_users` skips
  rows with NULL email. Round-trip the new `email` column.
- **Unit** (`tests/test_user_digest.py`): `build_user_digest` preserves the input (cosine) order,
  produces one "Top updates" section, and attaches the shared 1-10 relevance + bullets + reason to
  each item.
- **Unit** (`tests/test_deliver_all.py`): `deliver_all` with a fake sender — one user's send raises →
  that user counts as `failed`, the others still send; an empty-article user counts as
  `skipped_empty`; the returned `DeliverySummary` totals are correct; default sender is
  `PreviewEmailSender`.
- **CLI** (`tests/test_digest_user_cli.py`): `send-all` / `preview-all` are registered; preview path
  writes one HTML per user; hermetic (`STATE_STORE_KEY` set via monkeypatch, job/loader mocked — no
  network, no DB).
- **Gates:** `ruff check .` = 0; `ruff format --check .` clean; `pytest -m "not integration"` green
  incl. the above; coverage ≥ 80% (CI gate). `@db` tests run against the pgvector container on
  `localhost:5439`.

## Out of scope (future phases)

- Per-user send-time / timezone scheduling (v1 sends the whole batch at one 09:00 cron).
- Unsubscribe links / suppression list.
- "Already-emailed" dedupe table (a daily cron sends once/day — YAGNI for v1).
- Weekly cadence per user.
- Swapping `SmtpEmailSender` for a Resend/SES adapter for volume > ~500/day — already a drop-in port
  behind `EmailSender`; no redesign needed when it's time.

## Docs to update during implementation

- `SPEC.md` — note the `user_profiles.email` column and the per-user delivery path (no new
  invariant; this is additive delivery).
- `architecture.MD` — add `digest/user_source.py`, `digest/user_digest.py`, the `deliver_all`
  function, and `daily-digest-users.yml` to the module tree + data-flow.
- `planning.MD` — mark Phase 3.5 (per-user delivery) and its acceptance criteria.
- `DEPLOYMENT.md` — the one-time `ALTER TABLE user_profiles ADD COLUMN email text;` on live Neon and
  the new workflow's secrets/variables.
- Memory — record the per-user delivery path and the `email`-column contract.
