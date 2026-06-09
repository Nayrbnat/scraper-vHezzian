# TESTING.md — ScrapeForge testing strategy & unit-test plan

> The testing backbone is in place **before** product code (`pyproject.toml`, `.github/workflows/ci.yml`,
> `tests/`). Every module is built **test-first (TDD)** — the builder writes the failing test, then the
> implementation. CI enforces it: `ruff` + `pytest -m "not integration"` + **≥80% coverage**.

## 1. Test tiers

| Tier | Marker | Runs in CI? | Network | What it covers |
|---|---|---|---|---|
| **Unit** | (none) | ✅ every PR | mocked (`respx`) / none | logic in isolation — the bulk of the suite |
| **DB** | `@pytest.mark.db` | ✅ (PG service container) | local Postgres only | SQLAlchemy models, `PostgresSink`, repositories |
| **Integration** | `@pytest.mark.integration` | ❌ manual only | **live** sites/proxies | end-to-end against real targets; needs creds/proxies |

Run locally:
```bash
pytest -m "not integration"                  # what CI runs
pytest -m "not integration" --cov            # with coverage
pytest tests/test_storage.py::test_resume    # one focused test (TDD inner loop)
pytest -m integration                        # manual, with real .env + proxies
```

## 2. Principles

- **Mock the boundary, never the unit.** Mock the *network* (`respx` for HTTP, a fake nodriver service,
  a fake clock), not the function under test. A test that only asserts "the mock was called" is a smell.
- **Determinism.** No real time, no real sockets, no real Chrome in unit tests. Use `freezegun`
  (`frozen_clock` fixture) for delays/TTLs; ephemeral Postgres (`pytest-postgresql`) for `db` tests.
- **Cover the failure paths that matter most for a scraper:** soft-block detection, escalation ladder,
  resume-after-crash, fingerprint coherence, auth/encryption.
- **Async-first.** `asyncio_mode = auto` — write `async def test_...` directly; no decorator needed.
- **Integration tests are real acceptance, not CI.** They prove we actually defeat anti-bot; they run on
  an operator's machine with valid proxies, never in CI.

## 3. Shared fixtures (`tests/conftest.py`, extend as modules land)

`tmp_states_dir`, `fake_env` (throwaway Fernet key — never a real secret), `frozen_clock`, and an
autouse offline guard. Add per-area fixtures (mock `StealthBridge`, fake `ProxyRotator`, ephemeral DB
session) beside the tests that need them.

## 4. Per-module test matrix (the unit-test scaffolding plan)

Each module ships with its test file; key cases below. (Builders add cases as they discover edge paths.)

| Module | Test file | Key cases |
|---|---|---|
| `core/registry.py` | `test_registry.py` | decorator binds domain(s); duplicate registration raises; `get_scraper_for` exact→suffix match→`None`; `discover_scrapers` imports all packages |
| `core/stealth_bridge.py` | `test_stealth_bridge.py` | async dispatch to each backend; `async with` launches/closes; cookie round-trip; escalation detection (403 + `cf-mitigated`) |
| `core/drivers/*` | `test_drivers.py` | curl_cffi `AsyncSession` mocked via `respx`; `solve_challenge()` False for HTTP drivers; primp wrapped in `to_thread`; `close()` idempotent |
| `core/proxy_rotator.py` | `test_proxy_rotator.py` | health check (mock HTTP); burned-proxy exclusion + cooldown; session affinity; geo filter |
| `core/fingerprint_manager.py` | `test_fingerprint.py` | `detect_installed_chrome_version` (mock OS lookup); `curl_impersonate_target` maps major→alias; profile OS/UA/TLS coherence |
| `core/rate_limiter.py` | `test_rate_limiter.py` | per-domain interval enforced (`frozen_clock`); premium ≥60s floor; FIFO per domain |
| `core/storage.py` (`JsonlSink`) | `test_storage.py` | content-hash dedup; manifest resume after simulated crash; crash-safe append; `seen()` |
| `core/storage.py` (`PostgresSink`) | `test_postgres_sink.py` `@db` | upsert-by-id dedup; `seen()` = DB existence; implements `ArticleSink` contract |
| `core/engine.py` | `test_engine.py` | routes via `registry.get_scraper_for`; falls back to `PublicScraper`; circuit breaker open/close; rate-limit gating; sink write on success |
| `utils/validators.py` | `test_validators.py` | `response_is_valid` flags short/decoy/known-block-page fixtures; raises path |
| `utils/humanize.py` | `test_humanize.py` | bezier path non-linear; delay distribution within bounds; typing intervals Gaussian |
| `auth/state_store.py` | `test_auth.py` | Fernet encrypt/decrypt round-trip; TTL expiry; `filelock` concurrency; no plaintext on disk |
| `auth/sso_handler.py` | `test_auth.py` | interface mocked (no real headful login in CI); export/inject storage state |
| `scrapers/public/*` | `test_bucket3.py` | hybrid escalation (curl→patchright→curl); generic selector fallback chains |
| `scrapers/community/*` | `test_bucket2.py` | Reddit `.json` parse + pagination (`respx`); Substack paywall detection/escalation |
| `scrapers/premium/*` | `test_bucket1.py` | extraction from mock HTML; state injection; **no live paywall hits in CI** |
| `core/db/models.py` | `test_db_models.py` `@db` | schema round-trip; `articles` PK = sha256(url); `jobs` status transitions; JSONB metadata |
| `api/routes/articles.py` | `test_api_articles.py` | filter/paginate (`TestClient`); 404 on missing id; **401 without API key** |
| `api/routes/jobs.py` | `test_api_jobs.py` | `POST /jobs` enqueues (mock queue) → 202 + id; `GET /jobs/{id}` status; **API never calls a browser** |
| `api/auth.py` | `test_api_auth.py` | valid/invalid/missing key; per-key rate limit returns 429 |
| `worker/*` | `test_worker.py` | job consumed → `ScrapeEngine.batch_scrape` (mocked) → `PostgresSink` → status updated; failure marks job `error` |

## 5. Coverage policy

- Gate: **≥80%** branch coverage over non-integration tests (`--cov-fail-under=80` in CI).
- Excluded: `migrations/`, `tests/`, ellipsis-bodied spec stubs (see `[tool.coverage.report]`).
- Coverage is a floor, not a goal — prioritize the failure paths in §2 over chasing the last %.

## 6. CodeRabbit

CodeRabbit (`.coderabbit.yaml`) reviews PR diffs as a **second reviewer** and may *suggest* tests. It is
not a test runner and does not replace this suite or the adversarial `reviewer` agent — it augments them.
