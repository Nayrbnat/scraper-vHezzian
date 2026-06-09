# architecture.md — ScrapeForge Project Architecture
## Visual Directory Tree, Module Map & Extension Guide

> **Purpose:** At a glance, see exactly what functionality exists, where it lives, and how to add or remove modules. This is the structural companion to `SPEC.md` (the class-contract spec).

---

## 1. Directory Tree (Complete)

```
scrapeforge/
├── __init__.py
├── cli.py                          # Typer CLI: all user-facing commands
├── pyproject.toml                  # Dependencies, build config
├── README.md                       # Setup + usage guide
├── .env.example                    # Template for secrets & config
│
├── core/                           # === INFRASTRUCTURE LAYER ===
│   ├── __init__.py
│   ├── engine.py                   # ScrapeEngine: singleton orchestrator, circuit breaker (routes via registry)
│   ├── registry.py                 # ScraperRegistry: @register_scraper decorator + get_scraper_for (no central dict)
│   ├── stealth_bridge.py           # StealthBridge: unified ASYNC driver interface (4 backends)
│   ├── proxy_rotator.py            # ProxyRotator: health checks, session affinity, geo-targeting
│   ├── fingerprint_manager.py      # FingerprintManager: JA4/TLS/UA gen + installed-Chrome coherence
│   ├── rate_limiter.py             # RateLimiter: proactive per-domain politeness (async acquire)
│   ├── storage.py                  # ArticleSink / JsonlSink / PostgresSink: dedup, resume, RAG-ready
│   ├── drivers/                    # Driver implementations (hidden from scrapers)
│   │   ├── __init__.py
│   │   ├── base.py                 # BaseDriver: ABC all drivers inherit
│   │   ├── curl_cffi_driver.py     # CurlCffiDriver: HTTP/TLS impersonation (no JS)
│   │   ├── primp_driver.py         # PrimpDriver: Rust HTTP, OS-specific TLS
│   │   ├── patchright_driver.py    # PatchrightDriver: real Chrome via Playwright fork
│   │   └── nodriver_driver.py      # NodriverDriver: direct CDP WebSocket (nuclear option)
│   └── db/                         # === DATASTORE (shared by ingestion + serving) ===
│       ├── __init__.py
│       ├── models.py               # SQLAlchemy 2.0 async: articles, jobs, sources (+ pgvector embedding)
│       ├── session.py             # async engine + sessionmaker (DATABASE_URL)
│       └── repositories.py        # query helpers used by the API (filters, pagination)
│
├── api/                            # === SERVING PLANE (FastAPI: read + enqueue ONLY) ===
│   ├── __init__.py
│   ├── app.py                      # create_app() factory + middleware (auth, CORS, request-id)
│   ├── deps.py                     # DI: async DB session, settings, queue handle
│   ├── auth.py                     # API-key auth (X-API-Key) + per-key rate limit
│   ├── schemas.py                  # pydantic ArticleOut, JobIn, JobOut
│   └── routes/
│       ├── __init__.py
│       ├── articles.py             # GET /articles (filter+paginate), GET /articles/{id}
│       ├── jobs.py                 # POST /jobs (enqueue), GET /jobs/{id}, GET /jobs
│       ├── search.py               # POST /search (semantic, pgvector) — Phase-2 stub
│       └── health.py               # GET /health, GET /ready
│
├── worker/                         # === INGESTION WORKERS (arq) ===
│   ├── __init__.py
│   ├── main.py                     # WorkerSettings: consume queue → ScrapeEngine → PostgresSink
│   └── scheduler.py                # SchedulerSettings: arq cron enqueues recurring scrapes
│
├── migrations/                     # Alembic migrations (versioned schema)
│   ├── env.py
│   └── versions/
│
├── deployment/                     # === HOSTING (single VPS + Docker Compose) ===
│   ├── docker-compose.yml          # api, worker, scheduler, nodriver, postgres, redis, caddy
│   ├── Dockerfile.api              # slim, no browser
│   ├── Dockerfile.worker           # Python + system Chrome (patchright)
│   └── Caddyfile                   # reverse proxy + auto-HTTPS
│
├── scrapers/                       # === SCRAPER LAYER (Three Buckets) ===
│   ├── __init__.py                 # discover_scrapers() walks these packages so decorators run
│   ├── base.py                     # BaseScraper: ABC + batch_scrape + extraction helpers
│   │
│   ├── premium/                    # Bucket 1 — ONE FILE PER SITE (conflict-free; agents add files)
│   │   ├── __init__.py
│   │   ├── _base.py                # PremiumScraper base (shared; rarely edited)
│   │   ├── ft.py                   # @register_scraper('ft.com') FTScraper (+ optional cli/settings)
│   │   ├── bloomberg.py            # BloombergScraper
│   │   ├── wsj.py                  # WSJScraper
│   │   └── economist.py            # EconomistScraper
│   │
│   ├── community/                  # Bucket 2 — one file per platform
│   │   ├── __init__.py
│   │   ├── _base.py                # CommunityScraper base
│   │   ├── reddit.py               # RedditScraper (+ community Typer sub-app)
│   │   ├── substack.py             # SubstackScraper
│   │   └── wso.py                  # WSOScraper
│   │
│   └── public/                     # Bucket 3 — generic catch-all
│       ├── __init__.py
│       └── public.py               # PublicScraper: generic + fallback selectors
│
├── auth/                           # === AUTHENTICATION LAYER ===
│   ├── __init__.py
│   ├── manager.py                  # AuthManager: coordinates StateStore + SSOHandler
│   ├── state_store.py              # StateStore: encrypted Fernet vault for session states
│   └── sso_handler.py              # SSOHandler: interactive login (VNC / headful Chrome)
│
├── utils/                          # === UTILITIES ===
│   ├── __init__.py
│   ├── humanize.py                 # MousePathGenerator, ScrollSimulator, DelayEngine
│   ├── validators.py               # JA4 hash validation, response sanity checks
│   └── parsers.py                  # DOM extraction helpers (selectolax/BeautifulSoup wrappers)
│
├── config/                         # === CONFIGURATION ===
│   ├── __init__.py
│   └── settings.py                 # Pydantic Settings: env-driven, validated
│
├── exceptions.py                   # === EXCEPTION HIERARCHY ===
│   # ScrapeForgeError (base)
│   # ├── DriverError
│   # ├── AuthError
│   # ├── ProxyError
│   # ├── ChallengeError
│   # ├── RateLimitError
│   # └── FingerprintError
│
├── tests/                          # === TEST SUITE ===
│   ├── __init__.py
│   ├── conftest.py                 # Pytest fixtures: mock bridges, fake proxies, temp state stores
│   ├── test_stealth_bridge.py      # Driver dispatch, cookie round-trip, escalation logic
│   ├── test_engine.py              # Domain routing, circuit breaker, scraper registration
│   ├── test_proxy_rotator.py       # Health checks, burned proxy exclusion, rotation
│   ├── test_auth.py                # StateStore encryption, TTL expiry, SSO flow mocks
│   ├── test_bucket1.py             # Premium scraper mocks (DO NOT hit live paywalls in CI)
│   ├── test_bucket2.py             # Reddit JSON parsing, Substack archive parsing
│   ├── test_bucket3.py             # Public scraper + hybrid escalation logic
│   ├── test_humanize.py            # Bezier paths, delay distributions, scroll entropy
│   ├── test_validators.py          # response_is_valid soft-block detection, JA4 checks
│   ├── test_storage.py             # JsonlSink: dedup, resume manifest, crash-safe append
│   └── test_rate_limiter.py        # per-domain interval enforcement, premium floor
│
├── docs/                           # === DOCUMENTATION ===
│   ├── anti_detection.md           # Deep-dive: TLS, CDP leaks, behavioral evasion
│   ├── bucket1.md                  # Site-specific login instructions (FT, Bloomberg, WSJ)
│   ├── bucket2.md                  # Reddit API endpoints, Substack structure, WSO notes
│   ├── bucket3.md                  # Public site patterns, Cloudflare simple bypass
│   └── troubleshooting.md          # Common failures, debug checklist, fingerprint verification
│
└── services/                       # === OPTIONAL MICROSERVICES ===
    └── nodriver_service/           # AGPL isolation: nodriver as external HTTP service
        ├── Dockerfile
        ├── main.py                 # FastAPI: POST /session->token; /navigate,/cookies,/close take session_id
        ├── pyproject.toml          # Separate deps (nodriver, uvicorn)
        └── README.md               # Build & deploy instructions
```

---

## 2. Module Responsibility Map

| File | What It Does | Who Uses It | Add/Remove? |
|------|-------------|-------------|-------------|
| `core/engine.py` | Routes URLs to scrapers (via registry), manages circuit breakers, retry logic | CLI, batch scripts | **Core — never remove** |
| `core/registry.py` | `@register_scraper` decorator + `get_scraper_for(domain)`; scrapers self-register (conflict-free extension point) | Engine, all scrapers | **Core — never remove** |
| `core/stealth_bridge.py` | Unified interface over 4 drivers. All scrapers talk here. | All scrapers, SSOHandler | **Core — never remove** |
| `core/proxy_rotator.py` | Proxy health checks, session affinity, burned-IP cooldown | StealthBridge, Engine | **Core — never remove** |
| `core/fingerprint_manager.py` | Generates consistent browser profiles, validates JA4, derives curl impersonate target from installed Chrome | StealthBridge, CLI | **Core — never remove** |
| `core/rate_limiter.py` | Proactive per-domain politeness (async acquire); premium floor | Engine | **Core — never remove** |
| `core/storage.py` | `ArticleSink`/`JsonlSink`/`PostgresSink`: dedup, resume, RAG-ready output | Engine, scrapers, CLI, workers | **Core — keep** |
| `core/db/` | SQLAlchemy async models (articles/jobs/sources) + session + repositories | API, workers, `PostgresSink` | **Core (serving) — keep** |
| `api/` | FastAPI serving plane: read + enqueue only; never drives a browser | external clients | **Serving — keep** |
| `worker/` | arq workers + scheduler: consume jobs → `ScrapeEngine` → `PostgresSink` | queue | **Ingestion — keep** |
| `deployment/` | Docker Compose + Dockerfiles + Caddy (single-VPS host) | ops | **Infra — keep** |
| `core/drivers/base.py` | Abstract base for driver backends (async) | StealthBridge only | **Core — never remove** |
| `core/drivers/curl_cffi_driver.py` | HTTP/TLS impersonation via curl_cffi | StealthBridge | **Default — keep** |
| `core/drivers/primp_driver.py` | Rust HTTP client with OS-specific TLS | StealthBridge | **Optional — remove if unused** |
| `core/drivers/patchright_driver.py` | Real Chrome via Patchright fork | StealthBridge, SSOHandler | **Core — keep** |
| `core/drivers/nodriver_driver.py` | Direct CDP over WebSocket | StealthBridge | **Optional — isolate if AGPL concern** |
| `scrapers/base.py` | ABC: Article dataclass, batch logic, selector extraction | All bucket scrapers | **Core — never remove** |
| `scrapers/premium/*.py` | One file per premium site (FT, Bloomberg, WSJ, Economist) | Engine (via registry) | **Remove sites you don't need (delete file)** |
| `scrapers/community/*.py` | One file per platform (Reddit, Substack, WSO) | Engine (via registry) | **Remove platforms you don't need (delete file)** |
| `scrapers/public/public.py` | Generic public news scraper (catch-all) | Engine (via registry) | **Core — keep as fallback** |
| `auth/manager.py` | Coordinates login + state validation | PremiumScraper, CLI | **Remove if not scraping Bucket 1** |
| `auth/state_store.py` | Encrypted cookie vault | AuthManager, SSOHandler | **Remove if not scraping Bucket 1** |
| `auth/sso_handler.py` | Interactive browser login (VNC/headful) | AuthManager, CLI | **Remove if not scraping Bucket 1** |
| `utils/humanize.py` | Mouse paths, scroll entropy, delays | PatchrightDriver, WSOScraper | **Core — keep** |
| `utils/validators.py` | JA4 validation, response checks | FingerprintManager, tests | **Core — keep** |
| `utils/parsers.py` | DOM extraction wrappers | All scrapers | **Core — keep** |
| `config/settings.py` | Pydantic config, env loading | Everything | **Core — never remove** |
| `exceptions.py` | Typed exception hierarchy | Everything | **Core — never remove** |
| `cli.py` | Typer commands for all 3 buckets | User | **Core — keep** |

---

## 3. What's Included (Supported Targets)

### Bucket 1: Premium News Paywalls
| Site | Domain | Auth Required | Default Driver | Escalation Driver | Notes |
|------|--------|---------------|----------------|-------------------|-------|
| **Financial Times** | `ft.com` | Yes (SSO) | `patchright` | `nodriver` | Interactive login required. Session TTL: 7 days. |
| **Bloomberg** | `bloomberg.com` | Yes (SSO) | `patchright` | `nodriver` | Heavy Turnstile. Often requires nodriver. |
| **Wall Street Journal** | `wsj.com` | Yes (SSO) | `patchright` | `nodriver` | Dow Jones SSO. Session stable. |
| **The Economist** | `economist.com` | Yes (SSO) | `patchright` | `nodriver` | Lighter protection than Bloomberg. |

**To add a new premium site (conflict-free — one new file, no shared edits):**
1. Create a **new file** `scrapers/premium/<site>.py`.
2. Decorate the subclass: `@register_scraper('newsite.com', 'www.newsite.com')`; set `DOMAINS`.
3. Override `_get_selectors()` with domain-specific CSS.
4. (Optional) add a per-site settings fragment in the same file.
5. Add row to table above. **Do NOT edit `engine.py` / any central registry.**

---

### Bucket 2: Community & Foreign Sites
| Platform | Domain | Auth Required | Default Driver | Escalation Driver | Notes |
|----------|--------|---------------|----------------|-------------------|-------|
| **Reddit** | `reddit.com` | No (public API) | `curl_cffi` (JSON API) | `patchright` (deep comments) | Hits `/.json` endpoints. Rotate proxy per subreddit. |
| **Substack** | `*.substack.com` | No (public) / Yes (paid) | `curl_cffi` | `patchright` (paywall) | Public posts are server-rendered. Paid newsletters need JS. |
| **Wall Street Oasis** | `wallstreetoasis.com` | No | `nodriver` | — | Imperva protected. Heavy humanization required. |

**To add a new community platform (conflict-free — one new file, no shared edits):**
1. Create a **new file** `scrapers/community/<platform>.py`.
2. Decorate with `@register_scraper(...)`; set `DOMAINS` and `DEFAULT_DRIVER`.
3. Implement platform-specific methods (e.g., `scrape_subreddit()`, `scrape_archive()`).
4. (Optional) add a per-platform Typer sub-app + settings fragment in the same package.
5. Add row to table above. **Do NOT edit `engine.py` / `cli.py` / central `Settings`.**

---

### Bucket 3: Public News Outlets (Generic)
| Category | Examples | Auth Required | Default Driver | Escalation Driver | Notes |
|----------|----------|---------------|----------------|-------------------|-------|
| **WordPress blogs** | `*.wordpress.com` | No | `curl_cffi` | `patchright` (if CF challenge) | Generic selectors handle most. |
| **Small news sites** | Local outlets, trade pubs | No | `curl_cffi` | `patchright` (if CF challenge) | Hybrid pattern: solve once, curl resumes. |
| **Government / NGO** | `.gov`, `.org` releases | No | `curl_cffi` | Rarely needed | Usually no protection. |

**This bucket is domain-agnostic.** No registration needed for new public sites — `PublicScraper` handles them via generic CSS selectors and automatic escalation.

---

## 4. Driver Backend Matrix

> All drivers expose the **async** `BaseDriver` interface. curl_cffi uses `AsyncSession`,
> patchright uses `async_api`, nodriver is async-native, and primp (sync) is wrapped in
> `asyncio.to_thread`. Within a handoff chain all drivers share one `BrowserProfile` + proxy.

| Driver | File | JS Execution | TLS Impersonation | Speed | Use Case | Can Remove? |
|--------|------|-------------|-------------------|-------|----------|-------------|
| **curl_cffi** | `curl_cffi_driver.py` | No | Yes (JA4 + HTTP/2) | Fastest | Public sites, Reddit JSON, Substack public | **No** (default HTTP) |
| **primp** | `primp_driver.py` | No | Yes (OS-specific) | Very fast | Alternative to curl_cffi when OS fingerprint matters | Yes (optional) |
| **patchright** | `patchright_driver.py` | Yes | Yes (via real Chrome) | Medium | Premium paywalls, JS challenges, interactive login | **No** (default browser) |
| **nodriver** | `nodriver_driver.py` | Yes | Yes (via real Chrome) | Medium | Hard gates (Bloomberg, Imperva), Turnstile | Isolate if AGPL concern |

---

## 5. How to Add a New Scraper (Step-by-Step)

### Scenario A: Adding a New Premium Site (e.g., `nytimes.com`)

```python
# Step 1: Create ONE new file — scrapers/premium/nytimes.py. No other file is edited.
from core.registry import register_scraper

@register_scraper('nytimes.com', 'www.nytimes.com')   # self-registers on import
class NYTScraper(PremiumScraper):
    DOMAINS = ['nytimes.com', 'www.nytimes.com']
    DEFAULT_DRIVER = 'patchright'
    REQUIRES_AUTH = True

    def _get_selectors(self) -> dict:
        return {
            'title': 'h1[data-testid=headline]',
            'content': 'div[data-testid=article-body]',
            'author': 'span[data-testid=author]',
            'publish_date': 'time[datetime]',
        }

    async def scrape(self, url: str) -> ScrapeResult:
        # Optional: override for NYT-specific paywall logic
        return await super().scrape(url)
```

```text
# Step 2: There is NO engine edit. discover_scrapers() imports scrapers/premium/nytimes.py
#         at startup, so @register_scraper binds nytimes.com automatically.
```

```bash
# Step 3: Update docs
# Add row to architecture.MD Section 3 (Bucket 1 table)
# Add login notes to docs/bucket1.md
```

```bash
# Step 4: Test
python -m scrapeforge login --site nytimes.com --interactive
python -m scrapeforge scrape-premium --site nytimes.com --url https://www.nytimes.com/...
```

---

### Scenario B: Adding a New Community Platform (e.g., `hacker_news`)

```python
# Step 1: Create ONE new file — scrapers/community/hackernews.py
from core.registry import register_scraper

@register_scraper('news.ycombinator.com')
class HackerNewsScraper(CommunityScraper):
    DOMAINS = ['news.ycombinator.com']
    DEFAULT_DRIVER = 'curl_cffi'  # HN is static HTML
    REQUIRES_AUTH = False

    def _get_selectors(self) -> dict:
        return {
            'title': '.titleline > a',
            'content': None,  # HN has no body content on index
            'author': '.hnuser',
            'publish_date': '.age',
        }

    async def scrape_front_page(self, limit: int = 30) -> List[ScrapeResult]:
        # Custom method for HN's front page
        result = await self.scrape('https://news.ycombinator.com/')
        # Parse index page for story links
        ...
```

```text
# Step 2: No engine edit — the decorator self-registers on import.
```

---

### Scenario C: Adding a New Driver Backend

```python
# Step 1: Implement the driver
# File: core/drivers/new_driver.py

class NewDriver(BaseDriver):
    async def launch(self):
        ...
    async def navigate(self, url, wait_for, wait_until):
        ...
    async def get_html(self):
        ...
    # ... implement all abstract methods (all async) ...
```

```python
# Step 2: Register in StealthBridge
# File: core/stealth_bridge.py

def _init_backend(self) -> BaseDriver:
    if self.driver == 'newdriver':
        return NewDriver(self.proxy, self.profile, self.timeout_ms)
    # ... existing branches ...
```

```python
# Step 3: Add to CLI verify command
# File: cli.py
def verify_fingerprint(driver: str = typer.Option('curl_cffi', '--driver')):
    # Add 'newdriver' to allowed choices
    ...
```

---

## 6. How to Remove a Module

| If you want to remove... | What to do | Risk |
|--------------------------|-----------|------|
| **BloombergScraper** | Delete its file `scrapers/premium/bloomberg.py` (decorator goes with it) | Low. Other premium sites unaffected. |
| **RedditScraper** | Delete its file `scrapers/community/reddit.py` (decorator goes with it) | Low. Substack/WSO unaffected. |
| **PrimpDriver** | Delete `primp_driver.py`, remove branch from `StealthBridge._init_backend()` | Low. curl_cffi covers all HTTP needs. |
| **NodriverDriver** | Delete `nodriver_driver.py`, remove branch from `StealthBridge._init_backend()`, delete `services/nodriver_service/` | Medium. You lose nuclear option for hard gates. |
| **Auth system** | Delete `auth/` directory, remove `AuthManager` from `ScrapeEngine`, remove `login` CLI command | Medium. You can no longer scrape Bucket 1. |
| **SSOHandler** | Delete `auth/sso_handler.py`, remove `login` CLI command | Low if you never scrape premium sites. |

**Rule:** Deleting a scraper's file removes its `@register_scraper` binding automatically (no central
dict to clean up). Delete the file, then its tests. The catch-all `PublicScraper` covers any now-unmapped domain.

---

## 7. Data Flow Diagrams

> **Async note:** every `bridge.*`, `scraper.*`, `engine.*`, and `*_rotator` call below is `await`ed
> (omitted from arrows for readability). Bridges are entered via `async with`. Within a handoff chain
> all drivers reuse the **same `BrowserProfile` + same proxy** (Invariant #11). Successful results are
> persisted through `ArticleSink.write()` (the terminal step).

### 7.1 Bucket 1 (Premium) — Full Flow

```
User runs: scrapeforge login --site ft.com --interactive
    |
    v
SSOHandler.launch_interactive_browser('https://ft.com/login', driver='patchright')
    |---> StealthBridge(driver='patchright', headless=False)
    |---> PatchrightDriver opens VISIBLE Chrome window
    |---> User manually logs in
    |---> SSOHandler.export_storage_state(bridge)
    |---> StateStore.save(StorageState(domain='ft.com', cookies=[...]))
    |---> [ENCRYPTED] written to ~/.scrapeforge/states/ft.com.enc
    |
User runs: scrapeforge scrape-premium --site ft.com --url <article>
    |
    v
ScrapeEngine.scrape(url)
    |---> DomainRouter: 'ft.com' -> FTScraper
    |---> RateLimiter.acquire('ft.com')  [blocks >= 60s since last premium hit]
    |---> ProxyRotator.get_healthy_proxy() -> ProxySession
    |---> profile = FingerprintManager.generate_profile()  [SHARED across the chain]
    |---> FTScraper.scrape(url)
        |---> AuthManager.get_valid_state('ft.com') -> StorageState
        |---> async with StealthBridge(driver='patchright', proxy=P, profile=profile) as bridge
        |---> bridge.inject_storage_state(state)
        |---> bridge.navigate(url)   [PatchrightDriver, awaited]
        |---> [If Turnstile blocks] bridge.solve_challenge() -> FAIL
        |---> FTScraper._escalate_to_nodriver(url)   [SAME profile + SAME proxy P]
        |---> async with StealthBridge(driver='nodriver', proxy=P, profile=profile) as bridge
        |---> bridge.navigate(url)
        |---> [If nodriver also fails] -> TERMINAL: raise ChallengeError / prompt re-login
        |---> [Success] bridge.export_cookies()
        |---> FTScraper._handoff_to_curl_cffi(cookies)  [curl impersonate == Chrome major of profile]
        |---> async with StealthBridge(driver='curl_cffi', proxy=P, profile=profile) as bridge
        |---> [Polite fetch of remaining articles via curl_cffi]
        |---> _extract_article(html) -> Article   [raises ChallengeError if response_is_valid() False]
    |---> ScrapeResult(status='success', article=Article, driver_used='curl_cffi', ...)
    |---> ArticleSink.write(result)   [JSONL line + manifest append]
```

### 7.2 Bucket 2 (Community) — Reddit Flow

```
User runs: scrapeforge scrape-community --platform reddit --target wallstreetbets --limit 100
    |
    v
ScrapeEngine.scrape('https://www.reddit.com/r/wallstreetbets/.json?limit=100')
    |---> DomainRouter: 'reddit.com' -> RedditScraper
    |---> RateLimiter.acquire('reddit.com')
    |---> ProxyRotator.get_healthy_proxy() -> NEW proxy (per-subreddit rotation)
    |---> RedditScraper.scrape_subreddit('wallstreetbets', limit=100)
        |---> async with StealthBridge(driver='curl_cffi', proxy=P, profile=profile) as bridge
        |---> bridge.navigate('.../r/wallstreetbets/.json?limit=100')   [awaited]
        |---> [If 403/429] -> ChallengeError / RateLimitError
        |---> [Escalate] async with StealthBridge(driver='patchright', proxy=P, profile=profile)
        |---> bridge.navigate(url); bridge.solve_challenge() -> [If needed]
        |---> [If patchright fails Imperva-class gate] -> escalate nodriver -> else TERMINAL
        |---> [Success] parse JSON -> List[Article]   [response_is_valid gate]
    |---> List[ScrapeResult]
    |---> ArticleSink.write(result) per item  [sink.seen() skips already-done URLs on resume]
```

### 7.3 Bucket 3 (Public) — Hybrid Escalation Flow

```
User runs: scrapeforge scrape-public --source https://public-news.com/article
    |
    v
ScrapeEngine.scrape(url)
    |---> DomainRouter: unmapped domain -> PublicScraper (catch-all)
    |---> RateLimiter.acquire(domain)
    |---> ProxyRotator.get_healthy_proxy() -> P
    |---> profile = FingerprintManager.generate_profile()  [SHARED across the chain]
    |---> PublicScraper.scrape(url)
        |---> async with StealthBridge(driver='curl_cffi', proxy=P, profile=profile) as bridge
        |---> bridge.navigate(url)
        |---> [If 200 + response_is_valid()] -> _extract_article() -> Article
        |---> [If 403/CF challenge OR soft-block (200 decoy)] -> ChallengeError
        |---> PublicScraper._escalate_to_browser()   [SAME profile + SAME proxy P]
        |---> async with StealthBridge(driver='patchright', proxy=P, profile=profile) as bridge
        |---> bridge.navigate(url); bridge.solve_challenge() -> True (cf_clearance obtained)
        |---> bridge.export_cookies() -> [cf_clearance, session]
        |---> async with StealthBridge(driver='curl_cffi', proxy=P, profile=profile, cookies=exported)
              [curl impersonate == Chrome major used by patchright -> cf_clearance stays valid]
        |---> bridge.navigate(url) -> [200] -> Article
        |---> [If patchright fails to solve] -> TERMINAL: raise ChallengeError
    |---> ScrapeResult(status='success', article=Article, driver_used='curl_cffi', challenge_solved=True)
    |---> ArticleSink.write(result)
```

---

## 7.5 Serving Plane (API) & the two-plane architecture

ScrapeForge runs as **two decoupled planes over one Postgres**, so the browser-heavy ingestion never
blocks the lightweight, scalable read API.

```
  INGESTION PLANE (heavy, browser+proxies)            SERVING PLANE (light, stateless)
  arq worker → ScrapeEngine.batch_scrape              FastAPI (api/)
      → PostgresSink.write() ──────────►  Postgres (+pgvector)  ◄────── GET /articles, GET /articles/{id}
      ▲                                          ▲                       GET /jobs, GET /jobs/{id}
      │ consume job                              │ read                  POST /jobs ─┐
   Redis queue ◄─────────────────────────────────────────────────────── enqueue ◄──┘
   scheduler (arq cron) ── enqueues recurring scrapes
```

**Invariant #18 (decoupled planes):** the API is **read + enqueue only — it never drives a browser**.
A `POST /jobs` pushes a job to Redis; an `arq` worker runs the `ScrapeEngine` and writes results via
`PostgresSink` (which implements the existing `ArticleSink` seam). The API then serves stored rows.

### Endpoints

| Method · Path | Auth | Purpose |
|---|---|---|
| `GET /articles` | API key | List/filter stored articles (source, domain, bucket, date) + pagination |
| `GET /articles/{id}` | API key | One article by `id` (= sha256(url)); 404 if absent |
| `POST /jobs` | API key | Enqueue a scrape (`{source, urls?, bucket?, limit?}`) → `202 {job_id}` |
| `GET /jobs/{id}` | API key | Job status (`queued`/`running`/`done`/`error`) + result count |
| `GET /jobs` | API key | List recent jobs |
| `POST /search` | API key | **Phase-2 stub** — semantic search over `pgvector` |
| `GET /health`, `GET /ready` | none | Liveness / readiness for the load balancer |

All data endpoints require an `X-API-Key` header (per-key rate-limited). Hosting + ops: see `DEPLOYMENT.md`.

---

## 8. Configuration & Environment

### 8.1 Required Files (Create These Before First Run)

| File | Purpose | Template |
|------|---------|----------|
| `.env` | Secrets & config | See `.env.example` |
| `proxies.txt` | Proxy list | `http://user:pass@host:port` one per line |
| `~/.scrapeforge/states/` | Encrypted session storage | Auto-created by StateStore |

### 8.2 Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STATE_STORE_KEY` | Yes | — | Fernet encryption key (32+ chars, base64) |
| `PROXY_LIST_PATH` | No | `./proxies.txt` | Path to proxy list file |
| `LOG_LEVEL` | No | `INFO` | DEBUG, INFO, WARNING, ERROR |
| `LOG_FORMAT` | No | `text` | `text` or `json` |
| `SESSION_TTL_DAYS` | No | `7` | Days until stored session expires |
| `DEFAULT_OUTPUT_PATH` | No | `./output` | Base path for the JSONL sink + resume manifest |
| `HTML_PARSER` | No | `selectolax` | `selectolax` (lxml fallback) |
| `MIN_CONTENT_LENGTH` | No | `500` | Soft-block floor for `response_is_valid()` |
| `PREMIUM_MIN_INTERVAL_SECONDS` | No | `60` | Hard politeness floor for premium domains |
| `DEFAULT_RATE_INTERVAL_SECONDS` | No | `1.0` | Default per-domain min interval |

---

## 9. Testing Strategy by Module

| Module | Test File | Strategy | Live Network? |
|--------|-----------|----------|---------------|
| `StealthBridge` | `test_stealth_bridge.py` | Mock all 4 drivers, verify dispatch | No |
| `ProxyRotator` | `test_proxy_rotator.py` | Mock HTTP server for health checks | No |
| `StateStore` | `test_auth.py` | Temp dir + fake Fernet key | No |
| `FingerprintManager` | `test_validators.py` | Mock mitmproxy subprocess | No (unit) / Yes (integration) |
| `PublicScraper` | `test_bucket3.py` | Mock curl_cffi, mock patchright escalation | No |
| `RedditScraper` | `test_bucket2.py` | Mock `.json` response with `respx` | No |
| `FTScraper` | `test_bucket1.py` | Mock HTML + mock state injection | No |
| `Humanize` | `test_humanize.py` | Pure math/statistical tests | No |
| `JsonlSink` | `test_storage.py` | Temp dir: dedup, manifest resume, crash-safe append | No |
| `RateLimiter` | `test_rate_limiter.py` | Fake clock: interval enforcement, premium floor | No |
| `response_is_valid` | `test_validators.py` | Fixture HTML: decoy/block-page detection | No |
| **Integration** | `tests/integration/` | Hit 1 real site per bucket, low volume | **Yes — manual only** |

**Rule:** Integration tests are marked `@pytest.mark.integration` and SKIPPED in CI. They require manual execution with valid proxies and credentials.

---

## 10. Quick Reference: File -> Function

| If you want to... | Edit this file |
|---------------------|----------------|
| Add a new premium site | New file `scrapers/premium/<site>.py` + `@register_scraper` (no engine edit) |
| Add a new community platform | New file `scrapers/community/<platform>.py` + `@register_scraper` (no engine edit) |
| Add a new public site | Nothing — `PublicScraper` handles it automatically |
| Add a new driver | `core/drivers/new_driver.py` + `core/stealth_bridge.py` |
| Change proxy behavior | `core/proxy_rotator.py` |
| Change rate limiting / politeness | `core/rate_limiter.py` |
| Change output format / dedup / resume | `core/storage.py` |
| Change soft-block detection | `utils/validators.py` |
| Change login behavior | `auth/sso_handler.py` |
| Change session encryption | `auth/state_store.py` |
| Change humanization | `utils/humanize.py` |
| Change CLI commands | `cli.py` |
| Change config schema | `config/settings.py` |
| Add a new exception type | `exceptions.py` |

---

## 11. Anti-Pattern Checklist (Before Committing)

- [ ] No `sync_api` imports (use `patchright.async_api`)
- [ ] No `asyncio.run()` inside the running loop; wrap sync libs (`primp`) in `asyncio.to_thread`
- [ ] No `import requests` or `import httpx` (use `curl_cffi.AsyncSession` or `primp`)
- [ ] No `time.sleep(3)` (use `DelayEngine`); no blocking I/O on the event loop
- [ ] No plaintext cookie files (use `StateStore` with Fernet)
- [ ] No hardcoded passwords or API keys (use `.env` + `Settings`)
- [ ] No `nodriver` imported directly in main package (isolate in `services/` if AGPL concern)
- [ ] No central registry edits — scrapers self-register via `@register_scraper` (no `engine.py`/`cli.py`/`Settings` edits to add one)
- [ ] No missing `close()` on `StealthBridge` (use `async with`)
- [ ] No mismatched fingerprint across a handoff (same `BrowserProfile` + proxy; Chrome-aligned impersonate)
- [ ] No ad-hoc file writes (persist via `ArticleSink`); no success on a soft-block (`response_is_valid`)

---

## 12. Glossary

- **Bucket 1:** Premium news paywalls (FT, Bloomberg, WSJ, Economist). High security, SSO required.
- **Bucket 2:** Community/foreign sites (Reddit, Substack, WSO). Mixed security, API-first strategy.
- **Bucket 3:** Public news outlets. Low security, simple Cloudflare. Domain-agnostic.
- **CDP:** Chrome DevTools Protocol. The communication channel browsers expose. Leaked CDP signals = bot detection.
- **JA4:** TLS fingerprinting standard. Anti-bot systems hash the TLS handshake to identify automation tools.
- **Turnstile:** Cloudflare's invisible challenge. Requires real browser behavior or direct CDP evasion.
- **Stealth Bridge:** The unified abstraction layer over all browser/HTTP drivers.
- **Session Affinity:** Keeping the same IP/proxy/fingerprint for an entire user session (login -> browse -> scrape).
- **Hybrid Pattern:** Solving a JS challenge once with a browser, then exporting cookies to `curl_cffi` for fast parallel fetching.
- **Fingerprint Coherence:** Keeping JA4/UA/proxy identical across a handoff chain so an earned `cf_clearance` stays valid; the curl_cffi impersonate target is aligned to the installed Chrome major version (Invariant #11).
- **Soft Block:** An HTTP-200 decoy/honeypot page served instead of a 403. Caught by `response_is_valid()` and treated as a `ChallengeError`.
- **Resume Manifest:** A sidecar `<output>.manifest` of completed URL hashes that lets a crashed batch job skip already-scraped URLs (`ArticleSink.seen()`).
- **AGPL Isolation:** Running `nodriver` in a separate Docker service (session-keyed HTTP API) to avoid licensing contamination of the main codebase.