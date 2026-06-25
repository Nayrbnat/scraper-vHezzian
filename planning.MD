# ScrapeForge — Execution Plan & Roadmap
## planning.md

> **Version:** 1.0  
> **Last Updated:** 2026-06-05  
> **Owner:** Claude Code / Cloud Agent Development Team  
> **Status:** Draft → Ready for Implementation

---

## 0. Pre-Flight Checklist (Before Writing Code)

| Task | Owner | Deliverable | Time |
|------|-------|-------------|------|
| Verify Python 3.11+ installed | Dev | `python --version` | 5 min |
| Install Google Chrome (system, not Chromium) | Dev | Chrome on PATH | 10 min |
| Install `mitmproxy` for dev fingerprint validation | Dev | `mitmproxy --version` | 5 min |
| Create `.env` template with `STATE_STORE_KEY` | Dev | `.env.example` | 10 min |
| Prepare proxy list file (min 5 residential proxies) | Dev | `proxies.txt` | 15 min |
| Confirm AGPL-3.0 isolation strategy for `nodriver` | Legal/Arch | Decision doc | 30 min |
| Review target site ToS for all three buckets | Legal | Risk assessment | 1 hr |

**Gate:** Do not proceed to Phase 1 until all pre-flight items are complete.

---

## 1. Phase 1: Foundation & Core Infrastructure
**Goal:** Build the abstractions that all scrapers depend on. No scraping logic yet.
**Duration:** 3–4 days  
**Priority:** P0 (Blocking all other phases)

> ✅ **DELIVERED.** All foundation modules implemented TDD across work-units U1–U6: `exceptions.py`,
> `core/models.py`, `config/settings.py`, `utils/{humanize,parsers,validators}.py`,
> `core/{registry,rate_limiter,circuit_breaker,fingerprint_manager,proxy_rotator,engine}.py`,
> `core/storage/{base,jsonl}.py`, `core/drivers/{base,curl_cffi_driver}.py`, `core/stealth_bridge.py`,
> `scrapers/base.py`, `scrapers/public/public.py`, and the thin Typer CLI. **409 unit tests, 95% coverage**
> (`--cov-fail-under=80` restored in CI). primp/patchright/nodriver drivers and auth are deferred to later
> phases — the `_init_backend` factory raises a clear `DriverError` for them (honest seam).

### 1.1 Project Skeleton & Tooling
- [ ] Initialize `pyproject.toml` with all dependencies (patchright, nodriver, curl_cffi, primp, pydantic, typer, loguru, cryptography, **selectolax** (parser, decided up front — lxml fallback), **aiofiles** (async JSONL writes), **filelock**, pytest, pytest-asyncio, etc.).
- [ ] **Async-first decision:** the driver/bridge/scraper/engine layer is fully `async` (curl_cffi `AsyncSession`, `patchright.async_api`, native `nodriver`, `primp` via `asyncio.to_thread`). No `sync_api`, no `asyncio.run()` inside the loop. Configure `pytest-asyncio` accordingly.
- [ ] Set up `ruff` for linting/formatting.
- [ ] Set up `pytest` with `pytest-asyncio` and `conftest.py`.
- [ ] Create directory structure (`core/`, `scrapers/{premium,community,public}/`, `auth/`, `utils/`, `config/`, `tests/`, `services/`). **One file per scraper** so parallel agents never edit the same file.
- [ ] Implement `core/registry.py` (`@register_scraper` decorator, `get_scraper_for`, `discover_scrapers`) **in Foundation** — it is the conflict-free extension point every later scraper depends on (SPEC §3.16, Invariant #16).
- [ ] **Restore the coverage gate.** It is temporarily relaxed in CI (`--cov-fail-under` dropped) because the scaffold-only repo has no `scrapeforge/` package to measure. As soon as the first module lands here, add `--cov-fail-under=80` back to the `Unit tests` step in `.github/workflows/ci.yml` (matches CLAUDE.md §3 / §11 success metric).
- [ ] Add `.gitignore` (exclude `.env`, `.states/`, `__pycache__`, `.pytest_cache`).
- [ ] Create `README.md` with setup instructions and architecture diagram.

### 1.2 Configuration Layer
- [ ] Implement `config/settings.py` using `pydantic-settings`.
- [ ] Support `.env` file loading and env var overrides.
- [ ] Define all bucket-specific flags (`REDDIT_USE_JSON_API`, `SUBSTACK_USE_CURL_CFFI`, etc.).
- [ ] Add validation: `STATE_STORE_KEY` must be 32+ chars for Fernet.
- [ ] **Test:** Unit test config loading and validation.

### 1.3 Logging & Observability
- [ ] Configure `loguru` with structured JSON output in production.
- [ ] Add request/response logging middleware in `StealthBridge`.
- [ ] Implement correlation IDs for tracing a scrape request across driver switches.
- [ ] **Test:** Verify log output format.

### 1.4 Proxy Rotator (`core/proxy_rotator.py`)
- [ ] Parse proxy list from file (`protocol://user:pass@host:port`).
- [ ] Implement health check: pre-flight `GET` to `httpbin.org/ip` or similar.
- [ ] Implement session affinity: bind one proxy to one `StealthBridge` instance.
- [ ] Implement fallback chain: if health check fails, try next proxy before scrape.
- [ ] Add geo-targeting filter (optional country code matching).
- [ ] **Test:** Mock proxy server with `pytest-httpserver` or `respx`. Test health check and rotation logic.

### 1.5 Fingerprint Manager (`core/fingerprint_manager.py`)
- [ ] Define browser profile dataclass (TLS fingerprint, UA, HTTP/2 SETTINGS, accept-language, `chrome_major_version`).
- [ ] Implement profile generation for Chrome-on-Windows, Chrome-on-macOS, Chrome-on-Linux.
- [ ] Implement `detect_installed_chrome_version()` (Windows registry / `chrome --version` / Info.plist).
- [ ] Implement `curl_impersonate_target(profile)` mapping the installed Chrome major → curl_cffi alias (**fingerprint coherence, Invariant #11**).
- [ ] Ensure consistency: TLS fingerprint OS must match User-Agent OS; all drivers in a handoff chain share one profile + proxy.
- [ ] Implement `validate_ja4()` using `mitmproxy` in a subprocess.
- [ ] **Test:** Run `mitmproxy`, capture a `curl_cffi` request, and assert JA4 hash matches expected Chrome profile.

### 1.6 Humanization Utilities (`utils/humanize.py`)
- [ ] Implement cubic bezier mouse path generator with Perlin noise.
- [ ] Implement variable-velocity scroll with Gaussian acceleration.
- [ ] Implement typing simulation with Gaussian keypress intervals.
- [ ] Implement reading simulation (randomized 2–6s post-load pause).
- [ ] Implement `random_delay(min, max)` with jitter.
- [ ] **Test:** Unit test path generation (assert non-linear), delay distribution (assert within bounds).

### 1.7 Storage / Output Sink (`core/storage/`)
- [ ] Implement `ArticleSink` ABC (`async write`, `seen`, `async close`) in `core/storage/base.py` and `JsonlSink` in `core/storage/jsonl.py`.
- [ ] RAG-ready JSONL: one flattened article per line with stable `id = sha256(url)`.
- [ ] Content-hash dedup (skip re-writes of identical normalized content).
- [ ] Resume manifest: load `<output>.manifest` on init; append url-hash after each write.
- [ ] Use `aiofiles` for non-blocking appends.
- [ ] **Test (`test_storage.py`):** dedup, manifest resume after simulated crash, crash-safe append.

### 1.8 Rate Limiter (`core/rate_limiter.py`)
- [ ] Implement `RateLimiter.acquire(domain)` — per-domain min-interval / token bucket (async).
- [ ] Enforce premium hard floor (`PREMIUM_MIN_INTERVAL_SECONDS`, default 60s).
- [ ] Owned by `ScrapeEngine`; called before every dispatch.
- [ ] **Test (`test_rate_limiter.py`):** interval enforcement with a fake clock, premium floor, FIFO per domain.

### 1.9 Response Validators (`utils/validators.py`)
- [ ] Implement `response_is_valid(html, selectors, min_content_len)` — soft-block detection.
- [ ] Detect HTTP-200 decoy/honeypot pages: content floor, missing content selector, known block-page signatures (Cloudflare/Imperva "Just a moment...").
- [ ] Scrapers raise `ChallengeError` on `False` (feeds the normal escalation ladder).
- [ ] Implement `validate_ja4()` helpers used by FingerprintManager.
- [ ] **Test (`test_validators.py`):** fixture HTML for decoy/block-page detection.

### 1.10 Exception Hierarchy (`scrapeforge/exceptions.py`)
- [ ] Define `ScrapeForgeError` (base).
- [ ] Define `DriverError`, `AuthError`, `ProxyError`, `ChallengeError`, `RateLimitError`, `FingerprintError`.
- [ ] Ensure all scrapers raise typed exceptions.

### 1.11 Milestone: Foundation Complete
**Acceptance Criteria:**
- [ ] `pytest` passes for all Phase 1 tests.
- [ ] `python -m scrapeforge verify-fingerprint` works and prints a valid JA4 hash whose Chrome major matches the installed Chrome.
- [ ] Proxy rotator can bind, health-check, and rotate through 3 mocked proxies.
- [ ] Config loads from `.env` without errors.
- [ ] `JsonlSink` round-trips: write → kill → re-run skips completed URLs (resume works).
- [ ] `response_is_valid()` flags a known decoy/block-page fixture as invalid.

---

## 1.5. Phase 1.5: Thin Vertical Slice (De-Risk)
**Goal:** Prove the riskiest assumptions end-to-end *before* fully building every abstraction —
the async loop, the fingerprint, and the output path all working together on one real request.
**Duration:** 0.5–1 day
**Priority:** P0 (run as soon as config + a minimal async curl_cffi bridge + sink exist)

> ✅ **DELIVERED (U7).** `tests/integration/test_vertical_slice.py` (`@integration`, sandbox-only) drives one
> real `curl_cffi` fetch end-to-end: `ScrapeEngine → PublicScraper → StealthBridge[curl_cffi] → real TLS GET →
> validators → parsers → Article → JsonlSink (JSONL + manifest) → resume`. Passes live against
> `books.toscrape.com` (zero anti-bot / no DNS-block risk). The JA4/`verify-fingerprint` mitmproxy check is a
> documented MANUAL step (not run in CI). Plus U8: the **first curated real site** (Reddit via `.json`, Bucket 2)
> proving the per-site curation pipeline — see `docs/research/reddit.md`.

- [ ] Stand up a minimal `async` `CurlCffiDriver` + `StealthBridge` (`async with`) — no escalation yet.
- [ ] Scrape **one real public article** via `curl_cffi.AsyncSession` with a profile whose impersonate target matches the installed Chrome.
- [ ] Confirm the outbound JA4 via `verify-fingerprint` (mitmproxy) matches the claimed Chrome.
- [ ] Extract title/content with `selectolax`; gate with `response_is_valid()`.
- [ ] Write the result through `JsonlSink`; kill mid-run and confirm resume skips the completed URL.

**Acceptance:** one URL → valid JA4 → extracted Article → JSONL line + manifest entry → resumable.
Only after this passes do we build the full proxy/humanize/escalation stack against a known-good spine.

---

## 2. Phase 2: Bucket 3 — Public News (Low Security)
**Goal:** Implement the simplest bucket first. Validate the `StealthBridge` and `curl_cffi` integration.
**Duration:** 2–3 days  
**Priority:** P1 (Validates core abstractions)

### 2.1 Stealth Bridge v1 (`core/stealth_bridge.py`)
- [ ] Implement `curl_cffi` driver backend.
- [ ] Implement `primp` driver backend (optional, for OS-specific TLS).
- [ ] Implement the common **async** interface (`async navigate/get_html/export_cookies/import_cookies/close`, `__aenter__/__aexit__`); backend launched lazily in `__aenter__`.
- [ ] Implement automatic escalation detection: 403 + `cf-mitigated` header **or** soft-block (`response_is_valid()` False) → `ChallengeError`.
- [ ] **Test:** Mock `curl_cffi` with `respx`. Test interface and escalation detection.

### 2.2 Base Scraper (`scrapers/base.py`)
- [ ] Implement `BaseScraper` abstract class with `scrape()` and `batch_scrape()`.
- [ ] Implement generic `Article` dataclass (url, title, content, author, publish_date, raw_html, metadata).
- [ ] Implement `batch_scrape()` using `asyncio.gather` over a **pool of up to `max_concurrency` bridges, each with its own proxy** (not one shared session); bound by a semaphore. Skip URLs via `sink.seen()`; persist via `await sink.write()`.
- [ ] **Test:** Mock scraper subclass. Test batch concurrency limits and one-proxy-per-bridge.

### 2.3 Public Scraper (`scrapers/public/public.py`, `@register_scraper`)
- [ ] Implement `PublicScraper` inheriting from `BaseScraper`.
- [ ] Strategy: try `curl_cffi` first. On `ChallengeError`, escalate to `patchright`.
- [ ] Hybrid pattern: `patchright` solves challenge once, exports `cf_clearance`, hands back to `curl_cffi`.
- [ ] Implement DOM extraction helpers (title, body, publish date) using `BeautifulSoup` or `selectolax`.
- [ ] Add URL pattern matching for known public news domains.
- [ ] **Test:** Integration test against 2–3 real public news sites (low risk). Mark with `@pytest.mark.integration`.

### 2.4 CLI v1 (`cli.py`)
- [ ] Add `scrape-public` command: `--source`, `--output`, `--limit`.
- [ ] Add `verify-fingerprint` command.
- [ ] **Test:** Test CLI with `typer.testing.CliRunner`.

### 2.5 Milestone: Bucket 3 Complete
**Acceptance Criteria:**
- [ ] Can scrape 100 public articles using only `curl_cffi` with correct TLS fingerprint.
- [ ] Hybrid escalation works: if a site throws a challenge, `patchright` solves it and `curl_cffi` resumes.
- [ ] All tests pass. Integration tests skipped in CI but pass locally.

---

## 3. Phase 3: Bucket 2 — Community & Foreign Sites
**Goal:** Handle Reddit, Substack, and similar UGC platforms.
**Duration:** 3–4 days  
**Priority:** P1

### 3.1 Reddit Scraper (`scrapers/community/reddit.py`, `@register_scraper`)
- [ ] Research current Reddit internal JSON endpoints (verify `.json` still works in 2026).
- [ ] Implement `RedditScraper` with `curl_cffi` hitting `https://www.reddit.com/r/{sub}/.json`.
- [ ] Handle pagination via `after` token.
- [ ] Implement comment fetching: if top-level post data is insufficient, escalate to `patchright` for JS-rendered comment trees.
- [ ] Add proxy rotation per subreddit (new context per subreddit).
- [ ] Add rate limiting: exponential backoff with jitter on 429 responses.
- [ ] **Test:** Mock Reddit JSON with `respx`. Test pagination logic. Integration test against 1 subreddit (low volume).

### 3.2 Substack Scraper
- [ ] Implement `SubstackScraper`.
- [ ] Default to `curl_cffi` for public posts (usually server-rendered).
- [ ] Detect paywall: if content is truncated/encrypted, escalate to `patchright`.
- [ ] Implement newsletter archive listing (parse `/archive` page).
- [ ] **Test:** Mock Substack HTML. Test paywall detection and escalation.

### 3.3 Wall Street Oasis / Imperva Targets
- [ ] Implement `WSOScraper` (or generic `ImpervaScraper`).
- [ ] Use `nodriver` or `patchright` with extreme humanization.
- [ ] Implement slow navigation: 3–8s delays, bezier mouse paths, partial scroll + pause.
- [ ] Add session persistence: export cookies after successful navigation, reuse for subsequent pages.
- [ ] **Test:** This is high-risk. Do NOT automate integration tests against WSO in CI. Manual staging only.

### 3.4 Community CLI Commands
- [ ] Add `scrape-community` command with `--platform` (`reddit`, `substack`, `wso`), `--subreddit`, `--limit`.
- [ ] **Test:** CLI tests with mocked backends.

### 3.5 Milestone: Bucket 2 Complete
**Acceptance Criteria:**
- [ ] Reddit JSON scraper fetches 100 posts without 429s (using proxy rotation + delays).
- [ ] Substack public posts fetch via `curl_cffi`. Paywalled posts escalate correctly.
- [ ] WSO/Imperva scraper can navigate past initial challenge in manual staging.

### 3.6 Community Ingestion — Phase 1 (lean scheduled pipeline)

> ✅ **DELIVERED** (branch `feat/community-ingestion`). Daily scheduled ingestion of the 50
> curated investing-focused Substacks into Postgres via the `INGEST` queue:
>
> - **`IngestMessage`** contract added to `worker/messages.py`; `INGEST_QUEUE` setting added.
> - **`seed-substacks` CLI** (`community seed-substacks [--dry-run] [--limit N]`) atomically
>   upserts the 50 curated `Source` rows into the `sources` table (idempotent; `ON CONFLICT DO
>   UPDATE`).
> - **Scheduler routing** updated: `Source` rows carrying `params.platform` are published to the
>   `INGEST` queue; single-URL sources continue to the existing `JOB` queue unchanged.
> - **`community_ingest_worker`** (`worker/community_ingest_worker.py`) consumes each
>   `IngestMessage`, runs `SubstackScraper.scrape_publication`, archives each new post's raw
>   payload to the object store (claim-check/replay), and UPSERTs parsed rows via `PostgresSink`
>   (idempotent by `sha256(url)`). Owns the full Job lifecycle (queued → running → done | error).
> - **Deployment entry point** (`worker/run_community_ingest.py`) + `community-ingest` service
>   added to `deployment/docker-compose.yml`.
> - **Invariant #18 carve-out documented** in SPEC.md: the scraper→transform split governs
>   public-bucket HTML; community/JSON scrapers persist in-worker while still archiving raw.
>
> **Next phases:** per-article AI summaries → relevance ranking against user watchlist/themes →
> swipe-style daily brief UI (see Phase 9).

---

### AI Summaries + Relevance — Phase 2

> ✅ **DELIVERED** (branch `feat/article-summaries`). Per-article AI summarization: each
> article in Postgres is enriched with a 5-bullet investor summary and a 1–10 relevance score
> in a single LLM call (one call = one `UPDATE`). Built TDD:
>
> - **`core/llm/` port** (`base.py`, `openai_compatible.py`, `settings.py`, `exceptions.py`):
>   provider-agnostic `Summarizer` ABC + `SummaryResult` frozen dataclass; default adapter is
>   `OpenAICompatibleSummarizer` (httpx, JSON parse with lenient fallback, 429-retry stop,
>   1–10 clamping). Default LLM: **Zhipu GLM-4.5-Flash (free tier)**; swap by changing
>   `SUMMARY_API_*` env vars — no code change required.
> - **`SummarizerSettings`** (`core/llm/settings.py`): per-module `BaseSettings` fragment
>   (never extends core `Settings`); reads `SUMMARY_API_KEY` / `SUMMARY_API_BASE_URL` /
>   `SUMMARY_API_MODEL` / `SUMMARY_API_TIMEOUT` / `SUMMARY_BATCH_SIZE` / `SUMMARY_PORTFOLIO` /
>   `SUMMARY_INTERESTS` from `.env`. Key is never committed or logged.
> - **New `Article` columns**: `relevance INTEGER` (indexed, nullable, 1–10) and
>   `summary JSONB` (nullable; `{bullets, scores, reason, model, generated_at}`). Added via
>   `core/db/migrations.py` — idempotent `ADD COLUMN IF NOT EXISTS`, no Alembic dependency.
> - **`worker/summarize_worker.py`**: `summarize_pending()` batch cycle — `SELECT WHERE summary
>   IS NULL` → LLM call → `UPDATE`. Parse errors skip the row; rate-limit 429 stops the run.
>   `run_summarize_worker()` drains all pending in successive batches.
> - **`worker/run_summarize.py`**: deployment entry point (`python -m
>   scrapeforge.worker.run_summarize`), mirrors `run_transform.py`, calls migrations at startup.
> - **Compose service**: `summarizer` service added to `deployment/docker-compose.yml`.
> - **Invariant #18 carve-out documented** in SPEC.md §9: the summarizer (like community
>   ingest) is an explicit carve-out — it is a direct batch read-modify-write over Postgres,
>   not on the scraper→transform claim-check path.
>
> **Next phase:** Phase 4 — Swipe UI: the API exposes `relevance` + `summary` fields; the
> swipe-style brief UI consumes them to present one card per article, pre-ranked for the owner.

---

### Relevance Digest — Phase 2.5

> ✅ **DELIVERED** (branch `feat/digest-relevance`). The daily digest now renders the
> relevance-ranked `--source postgres` path alongside the existing `--source sample` CI path:
>
> - **`digest/relevance.py`** — `build_relevance_digest()`: pure filter (≥ `DIGEST_RELEVANCE_FLOOR`)
>   → sort (`relevance DESC`, `fetched_at` tiebreak) → cap at `DIGEST_TOP_N` → single `"top"` section.
> - **`digest/postgres_source.py`** — `load_ranked_articles()` async + `load_ranked_articles_sync()`
>   bridge; inlined read-only window SQL (not in `repositories.py`; seam rule).
> - **`digest/settings.py`** — `DigestSettings`: per-module `BaseSettings` fragment;
>   `DIGEST_RELEVANCE_FLOOR=5`, `DIGEST_TOP_N=10`, `DIGEST_WINDOW_HOURS=48`.
> - **`digest/models.py`** — `DigestItem` extended with `bullets: list[str]`, `relevance: int | None`,
>   `reason: str | None`; `DigestSection.key` extended to include `"top"`.
> - **`digest/renderer.py`** — bullets+badge+reason path when `item.bullets` non-empty; legacy
>   keyword path unchanged; `_badge_html()` coloured pill (green/amber/grey).
> - **`daily-digest.yml`** — `DIGEST_SOURCE` repo variable (defaults to `"sample"`; set to
>   `"postgres"` + add `DATABASE_URL` secret to activate ranked digest in production).
> - **Invariant #16 compliance:** `DigestSettings` is a per-module fragment; SQL is inlined in
>   `postgres_source.py`; no edits to `core/engine.py`, `repositories.py`, or root `Settings`.
>
> **Next phase:** Phase 4 — Swipe UI (see below).

---

### Multi-user Per-User Relevance — Phase 3

> ✅ **DELIVERED** (branch `feat/multiuser-relevance`). Per-user article ranking via embeddings +
> pgvector cosine similarity over the SHARED corpus — no per-user LLM call (Invariant #19). The
> Hezzian app owns/writes `user_profiles`; the pipeline only reads it and writes per-user scores.
>
> - **`core/embeddings/`** — `Embedder` port (`base.py`) + `GeminiEmbedder` (primary,
>   `gemini-embedding-001`, 1536-dim, key in `x-goog-api-key` header) + `OpenAICompatibleEmbedder`
>   (Jina fallback) + `make_embedder` factory + `EmbedderSettings` (`EMBED_*`) + typed exceptions.
> - **Schema (`core/db/models.py`)** — `user_profiles` (app-owned), `user_profile_vectors`,
>   `user_article_relevance` (FK→`articles.id` ON DELETE CASCADE, indexed `(user_id, score)`);
>   `articles.embedding Vector(1536)` reused. Created idempotently by `init-db`'s `create_all`.
> - **`pipeline/embeddings_jobs.py`** — `embed_articles` (WHERE embedding NULL), `embed_profiles`
>   (sha256 source-hash gate → skip unchanged), `score_users` (ORM `cosine_distance` top-K,
>   `score = 1 - distance`, no raw SQL), `seed_owner` (owner profile from `SUMMARY_*`).
> - **`pipeline/cli.py`** — `seed-owner` / `embed-articles` / `embed-profiles` / `score-users`;
>   embed commands idle-skip when `EMBED_API_KEY` is empty.
> - **`daily-pipeline.yml`** — four steps wired after `summarize`, before `prune`. New deploy
>   secret: **`EMBED_API_KEY`** (free Google AI Studio key) — until set, embed steps idle, pipeline
>   stays green.
> - **Cost:** $0 — embeddings on Gemini's free tier; scoring is pure in-database vector math.
> - **Invariant #16/#17/#19 compliance:** new files + named additive edits only (`models.py`,
>   `cli.py`, `daily-pipeline.yml`, docs); no edits to `engine.py`/`registry.py`/`repositories.py`/
>   core `Settings`/existing workers.
>
> **Out of scope (later):** login/feed UI, swipe feedback loop, per-user email delivery.

---

## 4. Phase 4: Bucket 1 — Premium News Paywalls
**Goal:** The hardest bucket. Bloomberg, FT, WSJ, Economist.
**Duration:** 5–7 days  
**Priority:** P1 (High business value, high complexity)

### 4.1 Auth & State Store (`auth/`)
- [ ] Implement `StateStore` with Fernet encryption.
- [ ] Domain-indexed storage: `~/.scrapeforge/states/ft.com.enc`, `~/.scrapeforge/states/bloomberg.com.enc`.
- [ ] TTL tracking: auto-invalidate sessions older than N days (configurable, default 7).
- [ ] Thread-safe file access using `filelock`.
- [ ] **Test:** Unit test encryption/decryption, TTL expiry, concurrent access.

### 4.2 SSO Handler (`auth/sso_handler.py`)
- [ ] Implement `launch_interactive_browser(url)` using `patchright` in headful mode.
- [ ] Support Camoufox VNC server as alternative (if user has it running).
- [ ] Implement `export_storage_state()` capturing cookies, localStorage, sessionStorage.
- [ ] Implement `load_storage_state()` injecting into new `patchright`/`nodriver` contexts.
- [ ] Add session validity checker: make a lightweight request to verify cookies still work.
- [ ] **Test:** Manual test only. Automate the interface, but do not automate actual SSO login in CI.

### 4.3 Premium Scraper (`scrapers/premium/_base.py` + one file per site, `@register_scraper`)
- [ ] Implement `PremiumScraper` inheriting from `BaseScraper`.
- [ ] Implement site-specific subclasses: `FTScraper`, `BloombergScraper`, `WSJScraper`.
- [ ] Login flow:
  1. Check `StateStore` for valid session.
  2. If invalid/missing, prompt user to run `python -m scrapeforge login --site ft.com --interactive`.
  3. After interactive login, store encrypted state.
  4. Load state into `StealthBridge` (patchright default).
- [ ] Article fetching:
  1. Use `patchright` with `channel=chrome` and injected session.
  2. If blocked by Turnstile, escalate to `nodriver`.
  3. Once past gate, export cookies to `curl_cffi` for bulk fetching.
- [ ] Implement article extraction logic for each site (CSS selectors, JSON-LD parsing).
- [ ] Add screenshot-on-failure for debugging (optional, config-driven).
- [ ] **Test:** Mock HTML for extraction logic. Manual integration tests for live sites.

### 4.4 Nodriver Microservice (If AGPL Isolation Required)
- [ ] If legal review requires isolation, create `services/nodriver_service/`.
- [ ] FastAPI server: `POST /session` → `session_id` token; `POST /navigate`, `POST /solve_challenge`, `GET /cookies`, `POST /close` all take `session_id` (the browser tab is stateful — the token correlates calls).
- [ ] Dockerfile with Chrome + nodriver + Python.
- [ ] Main project communicates via an **async** HTTP client holding the `session_id`.
- [ ] **Test:** Docker build test. Mock service in unit tests.

### 4.5 Premium CLI Commands
- [ ] Add `login` command: `--site`, `--interactive`, `--vnc`.
- [ ] Add `scrape-premium` command: `--site`, `--url`, `--output`, `--batch-file`.
- [ ] Add `list-sessions` command: show encrypted state store contents (domains + expiry).
- [ ] **Test:** CLI tests with mocked backends.

### 4.6 Milestone: Bucket 1 Complete
**Acceptance Criteria:**
- [ ] Interactive login exports valid session state for at least 2 premium sites.
- [ ] Automated fetch retrieves full article HTML behind paywall using stored session.
- [ ] Turnstile escalation to `nodriver` works in manual staging.
- [ ] All cookie states are encrypted at rest.
- [ ] No plaintext credentials in codebase.

---

## 5. Phase 5: Integration, Hardening & Polish
**Goal:** Tie everything together, add monitoring, documentation, and performance tuning.
**Duration:** 3–4 days  
**Priority:** P2

### 5.1 Engine & Orchestrator (`core/engine.py`)
- [ ] Implement `ScrapeEngine` that routes URLs via `core.registry.get_scraper_for(domain)` (calls `discover_scrapers()` at startup). No central domain dict on the engine.
- [ ] Implement retry logic with exponential backoff and **bounded** driver escalation (terminal `ChallengeError` / re-login prompt when the last rung fails — no infinite escalation).
- [ ] Call `RateLimiter.acquire(domain)` before every dispatch (proactive politeness).
- [ ] Wire the optional `ArticleSink`: `sink.seen()` to skip completed URLs, `await sink.write(result)` on success.
- [ ] Implement `CircuitBreaker` as its own module (`core/circuit_breaker.py`, symmetric with `RateLimiter`): if a site fails 5 times in 10 minutes, pause scrapes for 30 minutes. The engine owns an instance and calls `allow()`/`record()` — no breaker state inlined in the engine.
- [ ] **Test:** Unit-test engine routing/retry/sink-wiring and `CircuitBreaker` (trip + reset) separately; verify rate-limit gating.

### 5.2 Performance Tuning
- [ ] Profile `curl_cffi` batch scraping. Tune the per-domain bridge-pool size (default 5, allow override).
- [ ] Add connection pooling for `curl_cffi` `AsyncSession`s.
- [ ] Profile `selectolax` extraction hot paths (parser + async file I/O were decided up front in Phase 1 — no rework here).
- [ ] Tune `JsonlSink` batching/flush cadence under load.
- [ ] Benchmark: scrape 100 public articles in < 60 seconds.

### 5.3 Monitoring & Alerting
- [ ] Add Prometheus-compatible metrics: scrape_count, success_rate, challenge_escalation_count, proxy_failure_rate, soft_block_rate (per-domain), rate_limit_wait_seconds, sink_dedup_skips.
- [ ] Add health check endpoint (if running as service).
- [ ] Add structured error reporting: capture HTML snapshot + screenshot on failure (config-driven, respect privacy).

### 5.4 Documentation
- [ ] Complete `README.md` with architecture diagram, setup guide, and usage examples.
- [ ] Add `docs/bucket1.md` with site-specific login instructions.
- [ ] Add `docs/anti_detection.md` explaining the evasion strategy (TLS, CDP, behavioral).
- [ ] Add `docs/troubleshooting.md` for common failures (403, 429, session expiry).
- [ ] Add `CONTRIBUTING.md` with code conventions.

### 5.5 Final Testing & QA
- [ ] Run full test suite: `pytest -m "not integration"` must pass 100%.
- [ ] Run integration tests locally against all three buckets (low volume).
- [ ] Verify JA4 fingerprints for all drivers using `mitmproxy`.
- [ ] Verify no plaintext secrets in repo (`git-secrets` or `truffleHog` scan).
- [ ] Verify `nodriver` AGPL isolation boundary (if applicable).

### 5.6 Milestone: Production Ready
**Acceptance Criteria:**
- [ ] All three buckets functional via CLI.
- [ ] Test suite passes. Integration tests pass locally.
- [ ] No plaintext credentials. All states encrypted.
- [ ] Documentation complete.
- [ ] Performance benchmark met.

---

## 6. Phase 6: Serving API & Datastore
**Goal:** Turn the CLI scraper into a callable service — populate Postgres and expose a read + enqueue API.
**Duration:** 4–6 days
**Priority:** P1 (the product is "an API people call to retrieve data")

> ✅ **DELIVERED** as a full **event-driven pipeline** (not direct scraper→Postgres writes; see the revised
> Invariant #18). Built TDD across work-units W0–W9 (~165 new tests; full suite 574 passing, 94.5% cov):
> queue + object-store **ports** (`core/queue/`, `core/objectstore/`) with Redis + MinIO/S3 adapters
> (reliable-queue + DLQ; both validated against real Redis/MinIO); async datastore (`core/db/`) + Alembic;
> `PostgresSink`; **stateless scraper worker** (fetch → claim-check raw to object store → publish pointer,
> never touches the serving DB) and **idempotent transform worker** (read raw → normalize → UPSERT, sole
> writer of structured data + Job status); FastAPI **read+enqueue** API with X-API-Key auth (Invariant #18
> AST-enforced: no engine in routes); scheduler. Proven end-to-end: `tests/integration/test_pipeline_e2e.py`
> drives `POST /jobs → scraper → raw → transform → Postgres → GET /articles` with no-duplicate-on-rerun.
> Embeddings + working `/search` are deferred to the YouTube/RAG work (Phase 8). `docker compose config`
> validates (api / scraper-worker / transform-worker / scheduler / postgres / redis / minio / caddy).

### 6.1 Datastore (`core/db/`)
- [ ] SQLAlchemy 2.0 **async** models: `articles` (PK = sha256(url)), `jobs`, optional `sources` (SPEC §3.21).
- [ ] `core/db/session.py` (async engine + `async_sessionmaker` from `DATABASE_URL`).
- [ ] Alembic init + first migration under `migrations/`.
- [ ] **Test (`test_db_models.py` `@db`):** schema round-trip, PK dedup, job status transitions.

### 6.2 `PostgresSink` (`core/storage/postgres.py`)
- [ ] Implement `PostgresSink(ArticleSink)` — UPSERT by id (dedup), `seen()` = DB existence (resume).
- [ ] Engine/scrapers stay storage-agnostic; `JsonlSink` remains for local/CLI.
- [ ] **Test (`test_postgres_sink.py` `@db`):** upsert dedup, `seen()`, ArticleSink-contract parity.

### 6.3 Serving API (`api/`)
- [ ] `create_app()` factory + middleware (API-key auth, CORS, request-id, structured logging).
- [ ] Routes: `GET /articles` (filter+paginate), `GET /articles/{id}`, `POST /jobs` (enqueue),
      `GET /jobs/{id}`, `GET /jobs`, `GET /health|/ready`. `POST /search` is a Phase-2 stub.
- [ ] `api/auth.py`: `X-API-Key` validation + per-key rate limit (401/429).
- [ ] **Invariant #18:** routes never call `ScrapeEngine`/a browser — `POST /jobs` only enqueues.
- [ ] **Test (`test_api_*.py`):** happy path, 401 without key, 429 over limit, 404 missing id; assert no engine call from routes.

### 6.4 Workers & Scheduler (`worker/`)
- [ ] `arq` worker `run_scrape_job`: mark running → `ScrapeEngine(sink=PostgresSink).batch_scrape` → mark done/error.
- [ ] `scheduler.py`: arq cron enqueues recurring scrapes per `sources`.
- [ ] **Test (`test_worker.py`):** job consumed → engine (mocked) → sink → status updated; failure marks `error`.

### 6.5 Milestone: Service Complete
- [ ] `POST /jobs` enqueues; a worker scrapes and populates Postgres; `GET /articles` returns the rows.
- [ ] All data endpoints require a valid API key. API p95 read latency < 200 ms (local).

---

## 6.5 Phase 6.5: Live Deployment — Render + Neon (ACTIVE)
**Goal:** Ship a working, live-traffic deployment of the ingest → summarize → digest pipeline on
Render Cron Jobs backed by Neon serverless Postgres, without Redis or MinIO.
**Status:** 🔄 **ACTIVE — in progress.**

> **Deferred:** swipe app UI (Phase 9 / `Phase 7.6`) and protected object-store buckets remain
> deferred until the lean path is proven in production. Digest delivery by email ships first.

### What was built (delivered tasks)

| Deliverable | File(s) |
|-------------|---------|
| `pipeline/` sub-app with `init-db`, `ingest`, `summarize` commands | `scrapeforge/pipeline/{__init__,jobs,cli}.py` |
| `DATABASE_SSL` opt-in TLS (`_ssl_connect_args`) | `scrapeforge/core/db/session.py` |
| `make_engine` accepts explicit `database_url` arg | `scrapeforge/core/db/session.py` |
| `Settings.DATABASE_URL` added to core settings | `scrapeforge/config/settings.py` |
| `substack_sources.py` curated source list | `scrapeforge/scrapers/community/substack_sources.py` |
| SQLi-audit guard test | `tests/test_no_raw_sql.py` |
| Docs: pipeline sub-app + Render-cron path | `SPEC.md §3.26`, `architecture.MD §7.6`, this file |

### Render configuration (three Cron Jobs)

```
Service: init-db  (Deploy Hook — runs once on each new deploy)
  Command: scrapeforge pipeline init-db
  Env:     DATABASE_URL=<neon-url>  DATABASE_SSL=require

Service: ingest   (Cron: 0 */6 * * * — every 6 hours)
  Command: scrapeforge pipeline ingest --limit 25
  Env:     DATABASE_URL=<neon-url>  DATABASE_SSL=require  (+ CURL_CFFI_*)

Service: summarize (Cron: 0 3 * * * — daily 03:00 UTC)
  Command: scrapeforge pipeline summarize
  Env:     DATABASE_URL=<neon-url>  DATABASE_SSL=require  SUMMARY_API_KEY=<key>
```

### Milestone: Live on Render + Neon
- [ ] `scrapeforge pipeline init-db` completes successfully against Neon (SSL).
- [ ] `scrapeforge pipeline ingest --limit 5` inserts at least 1 article into Neon.
- [ ] `scrapeforge pipeline summarize` populates `relevance` + `summary` for those articles.
- [ ] `scrapeforge digest send --source postgres --yes` sends the daily email from live data.
- [ ] All three Render Cron Jobs scheduled and passing.

---

## 7. Phase 7: Deployment & Hosting (single VPS + Docker Compose)
**Goal:** Run both planes on one host; document the operational runbook.
**Duration:** 2–3 days
**Priority:** P2 — **deferred; use Phase 6.5 lean path first**

- [ ] `deployment/docker-compose.yml` (api, worker, scheduler, nodriver, postgres[pgvector], redis, caddy).
- [ ] `Dockerfile.api` (slim, no browser) + `Dockerfile.worker` (Python + system Chrome for patchright).
- [ ] `Caddyfile` (reverse proxy + auto-HTTPS); `.env.example` for all secrets.
- [ ] Run Alembic migrations on deploy; `/health`,`/ready` wired for the load balancer.
- [ ] `DEPLOYMENT.md`: VPS sizing, secrets, proxies, the **Bucket-1 interactive-login wrinkle** (local
      headful login → encrypted state → server `states/` volume), `pg_dump` backups, scaling workers, TLS.
- [ ] **Test:** `docker compose config` validates; smoke `GET /health` after `up`.

### 7.1 Milestone: Deployed
- [ ] Stack runs via `docker compose up`; API reachable over HTTPS; a scheduled scrape populates Postgres.

---

## 7.5. Phase 8: Bucket 4 — YouTube Transcripts (track → transcript → embed)
**Goal:** Track AI/finance YouTubers, detect new uploads, pull transcripts, clean → embed into pgvector.
**Priority:** P1 (new bucket the user requested) · **Status: SCHEDULED — blocked on the user's go-ahead to
clone/vendor the adopted repos.**

**Do NOT build from scratch.** Adopted stack (user decision): **youtube-transcript-api + scrapetube**
(primary), **yt-dlp + faster-whisper** (ASR fallback for PoToken/captions-disabled videos). Full shortlist,
2026 anti-block reality, and the architecture are in **`docs/research/youtube-transcripts.md`** (the Phase-8
playbook). Two-stage pipeline that **reuses the whole foundation** (engine seam, claim-check → transform →
PostgresSink): `track channels (scrapetube/RSS, dedup vs DB) → fetch transcript (youtube-transcript-api via
the residential ProxyRotator, wrapped in asyncio.to_thread) → clean → Article(content=transcript) → sink`.
Self-registers as `scrapers/youtube/` (Invariant #16). Hard constraint: **residential proxies mandatory**
(datacenter IPs are blocked); never run live YouTube fetches in CI (integration-only). Embeddings + working
`/search` (pgvector) land here, the first consumer that needs them.

- [ ] **Pre-req:** user confirms repos to clone + residential-proxy provider + GPU-for-Whisper decision + channel list.
- [ ] Upload detector (scrapetube/yt-dlp flat-playlist; RSS supplemental); dedup vs DB seen-set.
- [ ] `scrapers/youtube/` transcript scraper (youtube-transcript-api via ProxyRotator; PoToken/IpBlocked handling).
- [ ] ASR fallback (yt-dlp audio → faster-whisper), GPU-gated/queued.
- [ ] Embedding hook in the transform worker (no-op today) → real pgvector embeddings + working `POST /search`.

---

## 7.6. Phase 9: Personalized source aggregation + AI daily market brief (the Hezzian product layer)
**Goal:** Per onboarded user, take a **JSON profile** declaring which platforms they're connected to
(X, LinkedIn, Instagram, Substack, Reddit, YouTube, news…) + their **watchlist** (portfolio /
want-to-start-a-position companies) + their **industry themes** (deeply specific, e.g. *hybrid bonding →
TCNCF vs MUF-MDF, thermal compression*). Each connected source feeds its matching scraper bucket; all the
day's content is consolidated, and an **AI agent produces a daily brief of what CHANGED in the market**
for (1) the watchlist companies and (2) the tracked themes. **Priority:** P0 (this is the product).
**Status: PLANNED.** **Prototype = Dee's single JSON profile + Dee's credentials**, brief emailed daily to
nayrbnat@gmail.com.

**This extends the digest prototype** (`scrapeforge/digest/`, already built): the `Subscriber`/
`DigestPreferences` schema grows into the richer profile below, and the naive keyword matcher is replaced
by the LLM brief agent. Delivery reuses the existing `EmailSender` (preview → SMTP).

### 9.1 The user source-config JSON (the input — `data/profiles/<user>.json`)
The onboarding questionnaire ("what do you use?") produces this; for the prototype it's hand-authored for
Dee. Sketch (forward-compatible Pydantic models, same pattern as `Subscriber`):
```jsonc
{
  "id": "dee", "name": "Dee", "email": "nayrbnat@gmail.com",
  "sources": [                       // which platforms to connect + WHERE to pull from
    {"platform": "x",        "enabled": true,  "follow": {"accounts": ["@user1"], "lists": [], "search": ["hybrid bonding"]}, "credential_ref": "X"},
    {"platform": "substack", "enabled": true,  "follow": {"newsletters": ["semianalysis.com"]}},
    {"platform": "reddit",   "enabled": true,  "follow": {"subreddits": ["semiconductors"]}},
    {"platform": "youtube",  "enabled": true,  "follow": {"channels": ["<channel_id>"]}},
    {"platform": "linkedin", "enabled": false, "follow": {"companies": [], "people": []}, "credential_ref": "LINKEDIN"},
    {"platform": "instagram","enabled": false, "follow": {"accounts": []}, "credential_ref": "INSTAGRAM"}
  ],
  "watchlist": [                     // companies; held OR want-to-start-a-position
    {"name": "TSMC", "aliases": ["Taiwan Semiconductor", "2330.TW"], "stance": "watch"},
    {"name": "Besi", "aliases": ["BE Semiconductor"], "stance": "position"}
  ],
  "themes": [                        // hierarchical, deeply specific — drives day-over-day tracking
    {"name": "hybrid bonding", "keywords": ["hybrid bonding", "wafer-on-wafer", "die-to-wafer"],
     "subtopics": [
       {"name": "TCNCF", "keywords": ["thermal compression NCF", "TC-NCF"]},
       {"name": "MUF-MDF", "keywords": ["mass reflow MUF", "molded underfill"]}
     ]}
  ],
  "output": {"cadence": "daily", "format": "brief", "delivery": "email"}
}
```
`credential_ref` points to a key (encrypted `StateStore` once Phase 4 lands; `.env` for the prototype) —
**never inline secrets in the profile JSON.**

### 9.2 Source buckets (one self-registering scraper per platform → the existing pipeline)
Each platform is a new bucket that reuses the whole foundation (self-register via `@register_scraper`,
claim-check raw → transform → store). Difficulty + dependency varies sharply:
- [ ] **Easy / reachable now:** news (done), Reddit (done), Substack (Bucket 2), YouTube (Phase 8),
      **X** (via API / syndication endpoints where possible). Start the prototype here.
- [ ] **Hard / gated (defer):** **LinkedIn, Instagram** — require login + aggressive anti-bot + real
      ToS/account-ban + legal risk. Depend on **browser drivers (Phase 2)** + **auth/encrypted StateStore
      (Phase 4)** + **residential proxies**. Treat as a later, carefully-scoped add; never in CI.

### 9.3 Consolidation, dedup & tagging
All sources land in the article corpus (Postgres); entity-resolve + dedup (PK = sha256(url)), then tag each
item to watchlist companies + themes (keyword now; **semantic relevance via pgvector once Phase 8 embeddings
land** — that's the first real consumer of `/search`).

### 9.4 The AI brief agent (`scrapeforge/brief/` — extends the digest)
- [ ] Pull the day's items relevant to the profile (watchlist + themes; keyword + semantic filter).
- [ ] An **LLM (Claude)** synthesizes a structured daily brief: per-company "what changed today", per-theme
      "how it's evolving + what's new", each with **source citations**. Replaces the naive summarizer.
- [ ] **Day-over-day deltas, not a re-summary:** keep a per-company / per-theme running state (what was known
      yesterday) so the brief reports *changes*. This is the core of "what changes in the market each day."
- [ ] Deliver via the existing `EmailSender` (preview → SMTP), reusing the digest renderer.

### 9.5 Open decisions for Dee (resolve when prioritizing this phase)
- **First-prototype source set:** suggest X + Substack + Reddit + news + YouTube; defer LinkedIn/Instagram
  (hardest + legal). Confirm.
- **LLM model + budget** for the brief agent (Opus for synthesis quality vs Sonnet for cost; ~daily run cost).
- **Theme-tracking memory:** confirm the agent keeps per-theme/company state so briefs are deltas (implies a
  small state store of "last known" per entity).
- **Credentials/auth** for gated social sources (per-platform; needs StateStore + residential proxies; ToS risk).

**Dependencies:** digest prototype (built) + serving pipeline (Phase 6, built); browser drivers (Phase 2) +
auth/StateStore (Phase 4) for gated sources; embeddings (Phase 8) for semantic relevance.

---

## 8. Risk Register & Mitigation

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Reddit JSON API deprecated/closed | Medium | High | Fallback to `patchright` browser scraping already planned. Monitor API weekly. |
| `patchright` falls behind Playwright upstream | Low | Medium | Pin `patchright` to stable version. Monitor GitHub for updates. Have `nodriver` as fallback. |
| `nodriver` AGPL legal block | Medium | High | Isolate as microservice from Day 1. Do not link directly. |
| Premium sites change DOM/selectors | High | Medium | Use CSS selector fallback chains. Monitor with health checks. Alert on extraction failure. |
| Proxy IP reputation burned | High | Medium | Health checks before assignment. Rotate providers. Keep backup proxy list. |
| Cloudflare updates Turnstile to detect `nodriver` | Medium | High | Monitor anti-bot community (GitHub, ScrapingHub). Maintain fallback budget for new tool adoption. |
| Rate limits trigger account bans (Bucket 1) | Medium | High | `RateLimiter` enforces a hard ≥60s premium floor (not just guidance). Never batch premium. |
| cf_clearance invalidated by fingerprint skew on handoff | Medium | High | Invariant #11: one `BrowserProfile` + proxy per chain; curl impersonate aligned to installed Chrome major. |
| Site serves 200 decoy/honeypot instead of 403 | Medium | Medium | `response_is_valid()` soft-block detection; treated as `ChallengeError`; `soft_block_rate` metric alerts. |
| JA4 fingerprint drift (browser updates) | Medium | Medium | Monthly validation via `mitmproxy`. `curl_impersonate_target()` aligns the alias to the *installed* Chrome major version, not a stale pin. |
| Gated social sources (LinkedIn/Instagram/X) — login walls, aggressive blocking, ToS/legal, account bans (Phase 9) | High | High | Defer to a later, carefully-scoped add. Use encrypted `StateStore` + residential proxies; per-account rate floors; start the prototype with reachable sources (X API/syndication, Substack, Reddit, news, YouTube); never run live social fetches in CI. |
| AI brief hallucination / unsourced claims (Phase 9) | Medium | High | Every brief item must cite a stored source row; the LLM summarizes only retrieved corpus text, never free-generates facts; day-over-day deltas computed from stored state. |

---

## 9. Timeline Summary

| Phase | Duration | Cumulative | Key Deliverable |
|-------|----------|------------|-----------------|
| Phase 0: Test/CI backbone | 0.5 day | Day 0 | pyproject, CI (ruff+pytest+coverage), CodeRabbit, TESTING.md (tests-first) |
| Pre-Flight | 2–3 hrs | Day 1 | Environment ready, legal decision on AGPL |
| Phase 1: Foundation | 3–4 days | Day 5 | Core abstractions, registry, proxy, fingerprint, humanize, storage, rate limiter, validators |
| Phase 1.5: Vertical Slice | 0.5–1 day | Day 6 | One real URL end-to-end: async + JA4 + JSONL + resume |
| Phase 2: Bucket 3 | 2–3 days | Day 9 | Public news scraper, CLI v1 |
| Phase 3: Bucket 2 | 3–4 days | Day 12 | Reddit, Substack, WSO scrapers |
| Phase 3.6: Community ingestion Phase 1 | ✅ delivered | — | Lean scheduled ingestion of 50 curated Substacks → Postgres via INGEST queue; next: AI summaries → ranking → brief UI |
| AI Summaries + Relevance (Phase 2) | ✅ delivered | — | `core/llm/` port + GLM-4.5-Flash adapter; `relevance` INT + `summary` JSONB columns; batch summarize worker; idempotent migrations |
| Relevance Digest (Phase 2.5) | ✅ delivered | — | `digest/relevance.py` + `postgres_source.py` + `settings.py`; `DigestItem` bullets/relevance/reason; renderer badge+bullets path; `DIGEST_SOURCE` workflow variable; next: Phase 4 swipe UI |
| Multi-user Relevance (Phase 3) | ✅ delivered | — | `core/embeddings/` Embedder port (Gemini + Jina) + `pipeline/embeddings_jobs.py` (embed-articles/embed-profiles/score-users/seed-owner) + 3 contract tables (pgvector cosine, no per-user LLM); deploy secret `EMBED_API_KEY`; next: Phase 4 swipe UI / feed |
| **Live deployment — Render + Neon (Phase 6.5)** | **🔄 ACTIVE** | — | `pipeline/` sub-app (`init-db`/`ingest`/`summarize`); lean run-once Render Cron Jobs backed by Neon Postgres; `DATABASE_SSL` opt-in TLS; SQLi-audit guard (`tests/test_no_raw_sql.py`). Swipe app (Phase 9) and protected buckets deferred. |
| Phase 4: Bucket 1 | 5–7 days | Day 19 | Premium paywall scraper, auth system |
| Phase 5: Integration | 3–4 days | Day 23 | Engine, docs, performance, final QA |
| Phase 6: Serving API & Datastore | 4–6 days | Day 29 | Postgres, PostgresSink, FastAPI read+jobs, workers |
| Phase 7: Deployment | 2–3 days | Day 32 | Docker Compose on a VPS, runbook, scheduled scraping — **alternative to Phase 6.5 for high-volume/multi-source needs** |
| Phase 8: Bucket 4 — YouTube transcripts | 4–6 days | TBD | Track channels → transcripts (adopted libs) → embed (pgvector); blocked on repo go-ahead |
| Phase 9: Swipe app + personalized AI daily brief | 5–8 days | TBD | Per-user JSON profile → source buckets → consolidate → AI daily market brief (the product); prototype for Dee. **Deferred — digest email path ships first.** |
| **Buffer** | 3 days | **Day 35** | Contingency for anti-bot arms race |

**Total Estimated Duration:** ~5 weeks (1 developer full-time with Claude Code assistance).

---

## 10. Claude Code Execution Strategy

### 10.1 Recommended Agent Workflow
1. **Start each session** by loading `CLAUDE.md` (standards), `SPEC.md` (contracts), and `planning.MD` into context.
2. **Implement one module at a time.** Do not skip ahead. The Stealth Bridge must exist before any scraper.
3. **Write the failing test FIRST (TDD), then the module.** CI gates ruff + `pytest -m "not integration"` + ≥80% coverage (see `TESTING.md`).
4. **Use web search** to verify target site structures before writing selectors. Do not assume Reddit's DOM from 2024 is current.
5. **Use terminal** for: `pip install`, `mitmproxy` fingerprint validation, `pytest` execution, Docker builds.
6. **Use multi-file edit** when changing the `StealthBridge` interface (affects all drivers).

### 10.2 Skill Activation Recommendations
- **Web Search / Research:** Activate before implementing any site-specific scraper. Verify endpoints, selectors, and anti-bot status.
- **Terminal / Bash:** Activate for all testing and environment validation. Do not rely on simulated execution.
- **Code Review:** After completing each phase, run a self-review against the Anti-Patterns list in `architecture.MD §11` and the invariants in `SPEC.md §9`.
- **Documentation:** Use artifact generation for complex markdown files (architecture docs, troubleshooting guides).

### 10.3 Checkpoint Reviews
- **End of Phase 0:** CI green on a trivial PR; coverage gate + CodeRabbit active.
- **End of Phase 1:** Review `StealthBridge` interface. Ensure all 4 drivers are abstracted.
- **End of Phase 2:** Review Bucket 3 integration tests. Verify `curl_cffi` TLS fingerprint.
- **End of Phase 3:** Review Bucket 2 rate limiting. Verify no 429s in staging.
- **End of Phase 4:** Review AGPL isolation. Verify encryption. Manual login test on 2 premium sites.
- **End of Phase 5:** Full test suite + documentation review.
- **End of Phase 6:** API contract review; assert routes never call the engine; auth on all data endpoints.
- **End of Phase 7:** `docker compose config` clean; HTTPS reachable; scheduled scrape populates Postgres.

---

## 11. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Bucket 3 success rate | > 95% | % of public articles fetched without escalation |
| Bucket 3 fetch speed | < 600ms/article | Average `curl_cffi` fetch time |
| Bucket 2 Reddit success rate | > 90% | % of subreddit pages fetched without 429 |
| Bucket 1 session validity | > 7 days | Time until interactive re-login required |
| Bucket 1 article fetch | > 80% | % of paywalled articles fetched post-login |
| Soft-block detection | < 2% undetected | % of decoy/200 pages that slip past `response_is_valid()` (measured on fixtures + spot checks) |
| Batch resumability | 100% | Killed batch re-run scrapes 0 duplicate URLs (manifest dedup) |
| Output dedup | 0 dup ids | No duplicate `id`/content in the JSONL sink |
| Test coverage | ≥ 80% | `pytest --cov --cov-fail-under=80` (excl. integration) — enforced in CI once the first `scrapeforge/` module exists; temporarily relaxed while the repo is scaffold-only (see §1.1) |
| Code quality | 0 `ruff` errors | `ruff check .` + `ruff format --check .` (CI gate) |
| Secrets in repo | 0 | `gitleaks` in CI |
| API read latency | p95 < 200 ms | `GET /articles` under load (local) |
| Job throughput | ≥ 1 job picked < 2 s | enqueue → worker start latency |
| API auth | 100% | every data endpoint rejects missing/invalid key (401) |

---

## 12. Next Steps (Immediate Actions)

1. **Ensure `CLAUDE.md`, `SPEC.md`, `architecture.MD`, `GitHub.md`, and `planning.MD` are at project root.**
2. **Run Pre-Flight Checklist** (Section 0).
3. **Create `.env` file** with a generated Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
4. **Begin Phase 1.1:** Initialize `pyproject.toml` and project skeleton.
5. **Set up git repo** and make initial commit: `feat: initialize ScrapeForge project structure`.

**Ready to begin implementation.**
