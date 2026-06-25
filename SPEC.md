# SPEC.md — ScrapeForge Technical Specification
## For Claude Code: Class Contracts, Object Graphs & Implementation Rules
> **Note:** formerly `claude.md` — renamed to avoid colliding with the project `CLAUDE.md`
> (shared standards) on case-insensitive filesystems. `CLAUDE.md` is the operating manual; this is the
> object-model spec.

> **Purpose:** This document is the single source of truth for ScrapeForge's object model. Claude Code must read this before implementing any module. It defines every class, its attributes, methods, dependencies, and invariants.
> **Rule:** If a module's implementation contradicts this document, the document wins. Update the code, not the spec.

---

## 1. Object Graph (High-Level)

```
ScrapeEngine (singleton orchestrator)
├── ScraperRegistry (core/registry.py) — @register_scraper decorator; get_scraper_for(domain)
│       scrapers self-register on import; NO central DOMAIN_REGISTRY dict (Invariant #16)
├── RateLimiter — proactive per-domain politeness (async acquire)
├── CircuitBreaker (core/circuit_breaker.py) — reactive per-domain pause policy (allow/record)
├── FingerprintManager — installed-Chrome-derived profiles (coherence)
├── ArticleSink — JsonlSink (local/CLI) | PostgresSink (server); dedup + resume behind one seam
├── ProxyRotator — manages proxy lifecycle
│   └── ProxySession (dataclass: url, health_status, assigned_scraper)
├── StealthBridge (unified async driver interface)
│   ├── CurlCffiDriver
│   ├── PrimpDriver
│   ├── PatchrightDriver
│   └── NodriverDriver
├── AuthManager
│   ├── StateStore (encrypted vault)
│   └── SSOHandler (interactive login)
└── BucketScrapers
    ├── PremiumScraper (Bucket 1)
    │   ├── FTScraper
    │   ├── BloombergScraper
    │   ├── WSJScraper
    │   └── EconomistScraper
    ├── CommunityScraper (Bucket 2)
    │   ├── RedditScraper
    │   ├── SubstackScraper
    │   └── WSOScraper
    └── PublicScraper (Bucket 3)
        └── GenericPublicScraper (fallback for unmapped domains)
```

> The graph above is the **ingestion** object model. It runs inside `worker/` (arq), writing through
> `PostgresSink` into Postgres. The **serving** plane (`api/`, §3.22) is a *separate process* that reads
> that Postgres and enqueues jobs — it never instantiates `ScrapeEngine` (Invariant #18). See
> `architecture.MD §7.5` for the two-plane diagram.

---

## 2. Core Data Structures

### 2.1 `Article` (dataclass)
```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass(frozen=True, slots=True)
class Article:
    url: str
    title: str
    content: str                      # cleaned text or markdown
    author: Optional[str] = None
    publish_date: Optional[datetime] = None
    raw_html: Optional[str] = None    # optional, for debugging
    metadata: dict = field(default_factory=dict)
    # metadata keys: source_domain, bucket, driver_used, proxy_used,
    #                challenge_solved: bool, fetch_duration_ms: int
```

### 2.2 `ScrapeResult` (dataclass)
```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True, slots=True)
class ScrapeResult:
    # NOTE: required (non-default) fields MUST precede defaulted fields,
    # otherwise the dataclass raises TypeError at import time.
    status: str                       # 'success' | 'challenge' | 'rate_limited' | 'error' | 'proxy_failed'
    driver_used: str
    article: Optional[Article] = None
    error: Optional[str] = None
    proxy_used: Optional[str] = None
    challenge_solved: bool = False
    retry_count: int = 0
    fetch_duration_ms: int = 0
```

### 2.3 `BrowserProfile` (dataclass)
```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class BrowserProfile:
    # Single source of truth for a session's fingerprint. ALL drivers in one
    # handoff chain (browser -> curl_cffi) share the same BrowserProfile so the
    # JA4/UA/platform stay coherent (see Invariant #11).
    name: str               # e.g. 'chrome_win', 'chrome_mac', 'chrome_linux'
    user_agent: str
    tls_fingerprint: str    # JA4 hash or raw description
    http2_settings: dict    # SETTINGS_HEADER_TABLE_SIZE, etc.
    platform: str           # 'Win32' | 'MacIntel' | 'Linux x86_64'
    chrome_major_version: int  # resolved from installed Chrome; binds curl_cffi impersonate target
    accept_language: str = 'en-US,en;q=0.9'
    viewport: tuple[int, int] = (1920, 1080)
```

### 2.4 `ProxySession` (dataclass)
```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass(slots=True)
class ProxySession:
    url: str                # protocol://user:pass@host:port
    health_status: str = 'unknown'  # 'healthy' | 'unhealthy' | 'burned'
    last_used: Optional[datetime] = None
    failure_count: int = 0
    assigned_scraper: Optional[str] = None  # scraper class name
    country_code: Optional[str] = None
```

### 2.5 `StorageState` (dataclass)
```python
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


@dataclass(slots=True)
class StorageState:
    domain: str
    cookies: list[dict] = field(default_factory=list)
    local_storage: dict = field(default_factory=dict)
    session_storage: dict = field(default_factory=dict)
    # datetime.utcnow() is deprecated in 3.12+; use timezone-aware now().
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None  # TTL, default +7 days
    is_valid: bool = True
```

---

## 3. Core Classes — Full Specifications

> **Async convention (non-negotiable):** Every I/O method on `BaseDriver`, `StealthBridge`,
> `BaseScraper`, and `ScrapeEngine` is `async def` and must be `await`ed. There is exactly one
> event loop; **never** call `asyncio.run()` inside it. Async-native backends (`patchright.async_api`,
> `nodriver`, `curl_cffi.AsyncSession`) are used directly; sync-only backends (`primp`) are wrapped in
> `await asyncio.to_thread(...)`. Bridges launch their backend lazily on `__aenter__` and are used as
> `async with StealthBridge(...) as bridge:`.

### 3.1 `StealthBridge`
**File:** `core/stealth_bridge.py`
**Responsibility:** Abstract all driver differences. Every scraper talks to the bridge, never directly to Playwright/curl_cffi.

```python
from typing import Literal, Optional, List
from dataclasses import dataclass

class StealthBridge:
    # Unified interface over all automation drivers.
    #
    # Invariants:
    # - One StealthBridge instance = one proxy session (session affinity).
    # - Driver selection is immutable after construction.
    # - close() must be called before discard (deterministic cleanup).

    def __init__(
        self,
        driver: Literal['curl_cffi', 'primp', 'patchright', 'nodriver'],
        proxy: Optional[str] = None,
        profile: Optional[BrowserProfile] = None,
        headless: bool = True,
        timeout_ms: int = 30000,
    ) -> None:
        self.driver = driver
        self.proxy = proxy
        self.profile = profile or FingerprintManager().generate_profile('chrome')
        self.headless = headless
        self.timeout_ms = timeout_ms
        # Backend is constructed (config only) here; its connection/browser is
        # launched lazily in __aenter__ (async). _init_backend does NOT do I/O.
        self._backend: BaseDriver = self._init_backend()
        self._closed: bool = False

    async def __aenter__(self) -> 'StealthBridge':
        # Launch the backend (browser/session). Required before navigate().
        await self._backend.launch()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def navigate(
        self,
        url: str,
        wait_for: Optional[str] = None,
        wait_until: Literal['load', 'domcontentloaded', 'networkidle'] = 'domcontentloaded',
    ) -> ScrapeResult:
        # Navigate to URL and return result.
        # Raises: DriverError, ChallengeError
        ...

    async def get_html(self) -> str:
        # Return current page HTML.
        ...

    async def get_text(self, selector: str) -> Optional[str]:
        # Extract text from CSS selector. Returns None if not found.
        ...

    async def solve_challenge(self) -> bool:
        # Auto-detect and solve Cloudflare Turnstile / JS challenge.
        # For curl_cffi/primp: always returns False (no JS execution).
        # For patchright/nodriver: attempts human-like interaction.
        ...

    async def export_cookies(self) -> List[dict]:
        # Export cookies in Netscape-compatible format.
        # Returns: list of dicts with keys: name, value, domain, path, expires, httpOnly, secure.
        ...

    async def import_cookies(self, cookies: List[dict]) -> None:
        # Import cookies into current session.
        ...

    async def inject_storage_state(self, state: StorageState) -> None:
        # Inject cookies + localStorage + sessionStorage from a saved state.
        ...

    async def export_storage_state(self) -> StorageState:
        # Export current session state.
        ...

    async def screenshot(self, path: Optional[str] = None) -> bytes:
        # Capture screenshot. Returns PNG bytes. If path given, also saves to disk.
        ...

    async def close(self) -> None:
        # Deterministic cleanup. Idempotent (safe to call multiple times).
        ...

    def _init_backend(self) -> 'BaseDriver':
        # Factory: maps driver string to driver instance (config only, no I/O). Private.
        ...
```

**Dependencies:** `BaseDriver`, `BrowserProfile`, `FingerprintManager`, `StorageState`, `ScrapeResult`
**Used by:** All `BaseScraper` subclasses, `SSOHandler`

---

### 3.2 `BaseDriver` (Abstract Base)
**File:** `core/drivers/base.py`
**Responsibility:** Internal abstraction. StealthBridge delegates to this.

```python
from abc import ABC, abstractmethod
from typing import List, Optional

class BaseDriver(ABC):
    # Abstract base for all driver backends. Not exposed to scrapers.

    def __init__(self, proxy: Optional[str], profile: BrowserProfile, timeout_ms: int) -> None:
        self.proxy = proxy
        self.profile = profile
        self.timeout_ms = timeout_ms

    @abstractmethod
    async def launch(self) -> None: ...   # establish session/browser (async I/O), called by bridge __aenter__

    @abstractmethod
    async def navigate(self, url: str, wait_for: Optional[str], wait_until: str) -> ScrapeResult: ...

    @abstractmethod
    async def get_html(self) -> str: ...

    @abstractmethod
    async def get_text(self, selector: str) -> Optional[str]: ...

    @abstractmethod
    async def solve_challenge(self) -> bool: ...

    @abstractmethod
    async def export_cookies(self) -> List[dict]: ...

    @abstractmethod
    async def import_cookies(self, cookies: List[dict]) -> None: ...

    @abstractmethod
    async def inject_storage_state(self, state: StorageState) -> None: ...

    @abstractmethod
    async def export_storage_state(self) -> StorageState: ...

    @abstractmethod
    async def screenshot(self, path: Optional[str] = None) -> bytes: ...

    @abstractmethod
    async def close(self) -> None: ...
```

**Subclasses:** `CurlCffiDriver`, `PrimpDriver`, `PatchrightDriver`, `NodriverDriver`

---

### 3.3 `CurlCffiDriver`
**File:** `core/drivers/curl_cffi_driver.py`

```python
from curl_cffi.requests import AsyncSession

class CurlCffiDriver(BaseDriver):
    # HTTP-only driver using curl_cffi for TLS/HTTP2 impersonation.
    #
    # Invariants:
    # - No JavaScript execution. solve_challenge() always returns False.
    # - Session persists cookies across requests.
    # - impersonate target is derived from profile.chrome_major_version
    #   (FingerprintManager.curl_impersonate_target) so JA4 stays coherent with the
    #   browser driver that obtained any cf_clearance (Invariant #11). Never pin a
    #   stale version; align it with the installed Chrome.

    def __init__(self, proxy: Optional[str], profile: BrowserProfile, timeout_ms: int) -> None:
        super().__init__(proxy, profile, timeout_ms)
        self._impersonate = FingerprintManager().curl_impersonate_target(profile)
        self._session: Optional[AsyncSession] = None
        self._last_response = None

    async def launch(self) -> None:
        # Create the async session bound to proxy + impersonate target.
        self._session = AsyncSession(impersonate=self._impersonate, proxy=self.proxy)

    async def navigate(self, url: str, wait_for: Optional[str], wait_until: str) -> ScrapeResult:
        # Perform async GET request. Detect challenge by status code + headers.
        # Challenge detection:
        # - status == 403 and 'cf-mitigated' in response headers -> ChallengeError
        # - status == 403 and 'cloudflare' in response.text.lower() -> ChallengeError
        # - status == 429 -> rate limited
        ...

    async def get_html(self) -> str:
        # Return last response text.
        ...

    async def get_text(self, selector: str) -> Optional[str]:
        # Parse HTML with selectolax (lxml fallback), extract text.
        ...

    async def solve_challenge(self) -> bool:
        # Always returns False. No JS execution.
        return False

    async def export_cookies(self) -> List[dict]:
        # Convert curl_cffi cookie jar to Netscape-compatible list.
        ...

    async def import_cookies(self, cookies: List[dict]) -> None:
        # Load cookies into session.
        ...

    async def close(self) -> None:
        # Close session.
        if self._session is not None:
            await self._session.close()
```

---

### 3.4 `PatchrightDriver`
**File:** `core/drivers/patchright_driver.py`

```python
from patchright.async_api import async_playwright

class PatchrightDriver(BaseDriver):
    # Browser driver using Patchright (Playwright fork) with real Chrome.
    #
    # Invariants:
    # - ALWAYS launched with channel='chrome' (system Chrome, not bundled Chromium).
    # - CDP leak patches are automatic (Patchright handles this).
    # - Humanization applied to all interactions (mouse, scroll, typing).
    # - profile.chrome_major_version MUST match the launched Chrome (Invariant #11);
    #   FingerprintManager derives the profile from the installed Chrome.

    def __init__(self, proxy: Optional[str], profile: BrowserProfile, timeout_ms: int, headless: bool = True) -> None:
        super().__init__(proxy, profile, timeout_ms)
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def launch(self) -> None:
        # Launch browser with profile-matched args (async_playwright().start()).
        # Args must include:
        # - channel='chrome', headless=self.headless, proxy=self.proxy
        # - --disable-blink-features=AutomationControlled
        # - --disable-dev-shm-usage
        # - User-Agent / viewport / locale from self.profile
        ...

    async def navigate(self, url: str, wait_for: Optional[str], wait_until: str) -> ScrapeResult:
        # Navigate with humanized behavior:
        # 1. await page.goto(url, wait_until=wait_until)
        # 2. Random delay 2-6s (reading simulation, DelayEngine)
        # 3. If wait_for given, await page.wait_for_selector(wait_for)
        # 4. If challenge detected (Turnstile iframe), attempt solve_challenge()
        ...

    async def solve_challenge(self) -> bool:
        # Detect Cloudflare Turnstile iframe and attempt human-like solve.
        # Strategy:
        # 1. Locate iframe with title containing 'challenge' or 'widget'.
        # 2. Move mouse via bezier path to iframe center.
        # 3. Click with randomized delay.
        # 4. Wait up to 15s for cf_clearance cookie.
        # 5. If cookie appears, return True.
        ...

    async def get_text(self, selector: str) -> Optional[str]:
        # await page.query_selector(selector); await .inner_text() with error handling.
        ...

    async def close(self) -> None:
        # Close context, browser, playwright in reverse order.
        ...
```

---

### 3.5 `NodriverDriver`
**File:** `core/drivers/nodriver_driver.py`

```python
import nodriver as uc

class NodriverDriver(BaseDriver):
    # Direct CDP driver using nodriver. Nuclear option for hard gates.
    #
    # Invariants:
    # - Uses system Chrome via CDP WebSocket, no Playwright shim.
    # - nodriver is async-native: methods are `async def` and await nodriver directly.
    #   NEVER use asyncio.run() (it crashes inside the running loop).
    # - If AGPL isolation is active, this class is a thin async HTTP client to the
    #   nodriver microservice (holds a session_id; see services/nodriver_service).

    def __init__(self, proxy: Optional[str], profile: BrowserProfile, timeout_ms: int, headless: bool = True) -> None:
        super().__init__(proxy, profile, timeout_ms)
        self.headless = headless
        self._browser = None
        self._tab = None

    async def launch(self) -> None:
        # await uc.start(...) -> browser, bound to proxy + headless.
        ...

    async def navigate(self, url: str, wait_for: Optional[str], wait_until: str) -> ScrapeResult:
        # Native async nodriver tab navigation.
        # Steps:
        # 1. tab = await self._browser.get(url)
        # 2. Wait for DOM ready or network idle
        # 3. If wait_for given, await tab.select(wait_for) with timeout
        ...

    async def solve_challenge(self) -> bool:
        # Nodriver has no built-in challenge solver. Strategy:
        # 1. Wait 5-10s (many challenges auto-solve with real Chrome).
        # 2. Check for cf_clearance cookie.
        # 3. If present, return True.
        # 4. If not, return False (escalation failed -> terminal, see ladder note).
        ...

    async def close(self) -> None:
        # Stop nodriver browser.
        ...
```

---

### 3.6 `PrimpDriver`
**File:** `core/drivers/primp_driver.py`

```python
import asyncio
import primp

class PrimpDriver(BaseDriver):
    # Rust-based HTTP driver. Faster than curl_cffi, allows OS-specific TLS.
    #
    # Invariants:
    # - impersonate_os can be set independently ('windows', 'macos', 'linux').
    # - No JavaScript execution.
    # - primp is SYNCHRONOUS: every BaseDriver method wraps the blocking call in
    #   `await asyncio.to_thread(...)` so it never blocks the event loop.

    def __init__(self, proxy: Optional[str], profile: BrowserProfile, timeout_ms: int) -> None:
        super().__init__(proxy, profile, timeout_ms)
        self._client = primp.Client(
            impersonate='chrome',
            impersonate_os=self._os_from_profile(profile.platform),
        )

    async def launch(self) -> None:
        # primp.Client is created eagerly in __init__; nothing async to launch.
        return None

    async def navigate(self, url: str, wait_for: Optional[str], wait_until: str) -> ScrapeResult:
        # resp = await asyncio.to_thread(self._client.get, url)  # blocking call off-loop
        ...

    def _os_from_profile(self, platform: str) -> str:
        # Map platform string to primp OS string.
        ...
```

---

### 3.7 `BaseScraper`
**File:** `scrapers/base.py`

```python
from abc import ABC, abstractmethod
from typing import List, Optional
from asyncio import Semaphore

class BaseScraper(ABC):
    # Abstract base for all scrapers. Every scraper must inherit this.
    #
    # Invariants:
    # - Single-URL scrape() uses ONE StealthBridge bound to ONE proxy (session affinity).
    # - batch_scrape() does NOT share one session across concurrent URLs. It acquires a
    #   POOL of up to max_concurrency bridges from ProxyRotator, each with its own proxy,
    #   so concurrency spreads across IPs. Premium overrides max_concurrency=1 (one bridge).
    # - The semaphore bounds in-flight requests to the pool size.
    # - All scrapers must implement scrape().

    BUCKET: str = ''           # 'premium' | 'community' | 'public'
    DOMAINS: List[str] = []    # e.g. ['bloomberg.com', 'www.bloomberg.com']
    DEFAULT_DRIVER: str = 'curl_cffi'  # overridden per subclass

    def __init__(
        self,
        bridge: Optional[StealthBridge] = None,
        proxy: Optional[str] = None,
        max_concurrency: int = 5,
    ) -> None:
        self.bridge = bridge or self._create_default_bridge(proxy)
        self.proxy = proxy
        self._semaphore = Semaphore(max_concurrency)

    @abstractmethod
    async def scrape(self, url: str) -> ScrapeResult:
        # Scrape a single URL.
        # Must: navigate, extract Article, return ScrapeResult.
        # Should: handle ChallengeError, apply humanization delays.
        ...

    async def batch_scrape(self, urls: List[str]) -> List[ScrapeResult]:
        # Default: asyncio.gather over a pool of up to max_concurrency bridges,
        # each with its own proxy from ProxyRotator, bounded by self._semaphore.
        # Skips URLs already in the resume manifest (sink.seen(url)) and writes each
        # success via the configured ArticleSink. Subclasses may override for
        # bucket-specific batching (e.g. Reddit pagination).
        ...

    async def health_check(self) -> bool:
        # Verify target accessibility. Default: navigate to domain root, check status.
        ...

    def _create_default_bridge(self, proxy: Optional[str]) -> StealthBridge:
        # Factory: create StealthBridge with DEFAULT_DRIVER (config only; launch via async with).
        ...

    def _extract_article(self, html: str, url: str) -> Article:
        # Thin coordinator — owns NO parsing logic itself (SRP):
        #   1. utils.validators.response_is_valid(html, selectors) -> raise ChallengeError if soft-block.
        #   2. utils.parsers.extract(html, self._get_selectors()) -> field dict (DOM work lives there).
        #   3. Assemble the Article (a core data structure, SPEC §2.1) from those fields.
        # Subclasses override _get_selectors() for domain-specific CSS; they do not re-implement parsing.
        # Boundary: parsers = pure DOM->fields; validators = soft-block; base = coordinate + assemble.
        ...

    def _get_selectors(self) -> dict:
        # Return CSS selectors for this domain.
        # Expected keys: title, content, author, publish_date
        return {}
```

---

### 3.8 `PremiumScraper` (Bucket 1 Base)
**File:** `scrapers/premium/_base.py`

```python
class PremiumScraper(BaseScraper):
    # Base for all premium news paywalls.
    #
    # Strategy:
    # 1. Check StateStore for valid session.
    # 2. If valid, inject into StealthBridge (patchright default).
    # 3. Navigate to article. If Turnstile blocks, escalate to nodriver.
    # 4. Once past gate, export cookies to curl_cffi for bulk fetching.
    #
    # Invariants:
    # - NEVER script SSO password entry in headless mode.
    # - Session must be pre-warmed via interactive login (SSOHandler).
    # - DEFAULT_DRIVER is 'patchright' (not curl_cffi).

    BUCKET = 'premium'
    DEFAULT_DRIVER = 'patchright'
    REQUIRES_AUTH = True

    def __init__(self, bridge: Optional[StealthBridge] = None, proxy: Optional[str] = None, max_concurrency: int = 1):
        # Premium sites: concurrency = 1 to avoid detection
        super().__init__(bridge, proxy, max_concurrency=1)
        self.auth_manager = AuthManager()

    async def scrape(self, url: str) -> ScrapeResult:
        # Premium scrape flow:
        # 1. Load storage state for self.DOMAINS[0]
        # 2. bridge.inject_storage_state(state)
        # 3. bridge.navigate(url)
        # 4. If challenge: bridge.solve_challenge() or escalate to nodriver
        # 5. Extract article via _extract_article()
        # 6. Return ScrapeResult
        ...

    async def _escalate_to_nodriver(self, url: str) -> ScrapeResult:
        # Create new StealthBridge with nodriver, SAME profile + SAME proxy, re-inject
        # state, retry. Called only if patchright fails on Turnstile. If nodriver also
        # fails the challenge -> terminal: raise ChallengeError / prompt re-login.
        ...

    async def _handoff_to_curl_cffi(self, cookies: List[dict]) -> StealthBridge:
        # After authentication, create a curl_cffi bridge reusing the SAME BrowserProfile
        # (so impersonate target matches the Chrome that earned cf_clearance) and the SAME
        # proxy IP (Invariant #11). Used for bulk article fetching where polite limits allow.
        ...
```

**Subclasses:**
- `FTScraper` — `DOMAINS = ['ft.com', 'www.ft.com']`
- `BloombergScraper` — `DOMAINS = ['bloomberg.com', 'www.bloomberg.com']`
- `WSJScraper` — `DOMAINS = ['wsj.com', 'www.wsj.com']`
- `EconomistScraper` — `DOMAINS = ['economist.com', 'www.economist.com']`

---

### 3.9 `CommunityScraper` (Bucket 2 Base)
**File:** `scrapers/community/_base.py`

```python
class CommunityScraper(BaseScraper):
    # Base for community/foreign sites.
    #
    # Strategy:
    # - API-first (curl_cffi) for Reddit JSON, Substack static.
    # - Browser escalation (patchright/nodriver) for JS-rendered content or Imperva.
    # - Proxy rotation per subreddit/newsletter.
    #
    # Invariants:
    # - DEFAULT_DRIVER is 'curl_cffi'.
    # - max_concurrency can be higher (5-10) for API endpoints.

    BUCKET = 'community'
    DEFAULT_DRIVER = 'curl_cffi'
    REQUIRES_AUTH = False

    def __init__(self, bridge: Optional[StealthBridge] = None, proxy: Optional[str] = None, max_concurrency: int = 5):
        super().__init__(bridge, proxy, max_concurrency)

    async def scrape(self, url: str) -> ScrapeResult:
        # Community scrape flow:
        # 1. Try curl_cffi first.
        # 2. If 403/429/challenge detected, escalate to patchright with humanization.
        # 3. If patchright fails on Imperva, escalate to nodriver.
        # 4. Extract content.
        ...
```

**Subclasses:**
- `RedditScraper` — `DOMAINS = ['reddit.com', 'www.reddit.com']`
  - `scrape_subreddit(subreddit: str, limit: int = 100)` — hits `.json` endpoint
  - `scrape_comments(post_url: str)` — escalates to browser for deep threads
- `SubstackScraper` — `DOMAINS = ['substack.com', '*.substack.com']`
  - `scrape_post(url: str)` — curl_cffi for public, patchright for paywalled
  - `scrape_archive(newsletter_domain: str)` — parse `/archive` page
- `WSOScraper` — `DOMAINS = ['wallstreetoasis.com', 'www.wallstreetoasis.com']`
  - Heavy humanization required. Uses nodriver for initial challenge.

---

### 3.10 `PublicScraper` (Bucket 3)
**File:** `scrapers/public/public.py`

```python
class PublicScraper(BaseScraper):
    # Generic scraper for public news outlets.
    #
    # Strategy:
    # - curl_cffi as default (80% of targets).
    # - Hybrid escalation: patchright solves challenge once, curl_cffi resumes.
    #
    # Invariants:
    # - DEFAULT_DRIVER is 'curl_cffi'.
    # - max_concurrency = 5 (configurable).
    # - Domain-agnostic: uses generic CSS selectors with fallback chains.

    BUCKET = 'public'
    DEFAULT_DRIVER = 'curl_cffi'
    REQUIRES_AUTH = False

    def __init__(self, bridge: Optional[StealthBridge] = None, proxy: Optional[str] = None, max_concurrency: int = 5):
        super().__init__(bridge, proxy, max_concurrency)

    async def scrape(self, url: str) -> ScrapeResult:
        # Public scrape flow:
        # 1. curl_cffi GET url.
        # 2. If success (200, no challenge): extract article.
        # 3. If challenge detected:
        #    a. Create patchright bridge.
        #    b. Navigate, solve challenge.
        #    c. Export cookies.
        #    d. Create new curl_cffi bridge with cookies.
        #    e. Retry URL.
        # 4. Return result.
        ...

    def _get_selectors(self) -> dict:
        # Generic fallback selectors (work for many WordPress/news sites):
        # - title: 'h1.entry-title, h1.article-title, h1.post-title, h1'
        # - content: 'div.entry-content, article, div.post-content, div.content'
        # - author: 'span.author, a[rel=author], .byline'
        # - publish_date: 'time[datetime], span.date, .published'
        ...
```

---

### 3.11 `AuthManager`
**File:** `auth/manager.py`

```python
class AuthManager:
    # Coordinates authentication for all premium scrapers.
    #
    # Invariants:
    # - One AuthManager per ScrapeEngine.
    # - Delegates to StateStore for persistence and SSOHandler for interactive flows.

    def __init__(self, state_store: Optional[StateStore] = None) -> None:
        self.store = state_store or StateStore()
        self.sso = SSOHandler()

    def get_valid_state(self, domain: str) -> Optional[StorageState]:
        # Retrieve state from store. Check TTL. Return None if expired/invalid.
        ...

    async def is_session_valid(self, domain: str) -> bool:
        # Lightweight check: make an async HEAD/GET to domain root with stored cookies.
        ...

    async def prompt_interactive_login(self, domain: str) -> StorageState:
        # Launch interactive browser for user login. Await until user signals completion.
        # Returns exported state.
        ...
```

---

### 3.12 `StateStore`
**File:** `auth/state_store.py`

```python
from cryptography.fernet import Fernet
from pathlib import Path

class StateStore:
    # Encrypted vault for browser session states.
    #
    # Invariants:
    # - All files encrypted with Fernet (AES-128-CBC + HMAC).
    # - File naming: {domain}.enc in STATE_STORE_PATH.
    # - Thread-safe via filelock.

    def __init__(self, key: Optional[str] = None, store_path: Optional[Path] = None) -> None:
        self.key = key or Settings().STATE_STORE_KEY
        self.fernet = Fernet(self.key.encode())
        self.store_path = store_path or Settings().STATE_STORE_PATH
        self.store_path.mkdir(parents=True, exist_ok=True)

    def save(self, state: StorageState) -> Path:
        # Serialize state to JSON, encrypt, write to disk.
        ...

    def load(self, domain: str) -> Optional[StorageState]:
        # Read file, decrypt, deserialize. Return None if not found.
        ...

    def delete(self, domain: str) -> bool:
        # Remove state file. Return True if existed.
        ...

    def list_domains(self) -> List[str]:
        # Return all domains with stored state.
        ...

    def is_expired(self, state: StorageState) -> bool:
        # Check if state.expires_at is past.
        ...
```

---

### 3.13 `SSOHandler`
**File:** `auth/sso_handler.py`

```python
class SSOHandler:
    # Manages interactive login workflows.
    #
    # Invariants:
    # - Always opens a visible (headful) browser window.
    # - Supports Patchright headful and Camoufox VNC server.
    # - Never scripts password entry for premium targets.

    def __init__(self) -> None:
        self._active_bridge: Optional[StealthBridge] = None

    async def launch_interactive_browser(
        self,
        url: str,
        driver: Literal['patchright', 'camoufox'] = 'patchright',
        proxy: Optional[str] = None,
    ) -> StealthBridge:
        # Launch a visible browser for manual user login.
        # Args:
        #   url: Login page URL.
        #   driver: 'patchright' (headful Chrome) or 'camoufox' (VNC Firefox).
        #   proxy: Optional proxy for the login session.
        # Returns: a launched (async with-entered) StealthBridge, headful, ready for interaction.
        ...

    async def export_storage_state(self, bridge: StealthBridge) -> StorageState:
        # Capture current cookies, localStorage, sessionStorage from bridge.
        ...

    async def close(self) -> None:
        # Close active bridge.
        ...
```

---

### 3.14 `ProxyRotator`
**File:** `core/proxy_rotator.py`

```python
class ProxyRotator:
    # Manages proxy lifecycle with health checks and session affinity.
    #
    # Invariants:
    # - One proxy per StealthBridge instance.
    # - Proxies are health-checked before assignment.
    # - Failed proxies are marked 'burned' and excluded for 1 hour.

    def __init__(self, proxy_list_path: Optional[Path] = None) -> None:
        self.proxies: List[ProxySession] = []
        self._load_proxies(proxy_list_path or Settings().PROXY_LIST_PATH)

    async def get_healthy_proxy(
        self,
        country_code: Optional[str] = None,
        exclude_burned: bool = True,
    ) -> Optional[ProxySession]:
        # Return a healthy proxy matching criteria (health-checks candidates).
        # Args:
        #   country_code: Optional ISO country code filter.
        #   exclude_burned: If True, skip proxies with health_status='burned'.
        # Returns: ProxySession or None if no healthy proxies available.
        ...

    async def health_check(self, proxy: ProxySession) -> bool:
        # Test proxy by making an async request through it to PROXY_HEALTH_CHECK_URL.
        # Returns True if response is 200 and IP is not blacklisted.
        ...

    def mark_burned(self, proxy: ProxySession) -> None:
        # Mark proxy as burned. It will be excluded for 1 hour.
        ...

    def release(self, proxy: ProxySession) -> None:
        # Release proxy back to pool (update last_used).
        ...

    def _load_proxies(self, path: Path) -> None:
        # Parse proxy list file. Format: protocol://user:pass@host:port
        ...
```

---

### 3.15 `FingerprintManager`
**File:** `core/fingerprint_manager.py`

```python
class FingerprintManager:
    # Generates and validates browser fingerprints.
    #
    # Invariants:
    # - Profiles are consistent (TLS + UA + platform match).
    # - The curl_cffi impersonate target is DERIVED from the installed Chrome major
    #   version, never a stale pin. This keeps the HTTP driver's JA4 coherent with the
    #   browser driver across a handoff chain (Invariant #11).

    PROFILES: dict[str, BrowserProfile] = {
        'chrome_win': BrowserProfile(...),
        'chrome_mac': BrowserProfile(...),
        'chrome_linux': BrowserProfile(...),
    }

    def detect_installed_chrome_version(self) -> int:
        # Resolve the installed system Chrome major version (cross-platform:
        # Windows registry / `chrome --version` / Info.plist). Cached per process.
        ...

    def generate_profile(self, name: str = 'chrome') -> BrowserProfile:
        # Return a profile with chrome_major_version stamped from
        # detect_installed_chrome_version(). If name is generic, pick an OS variant
        # matching the host (or a requested one). All drivers in a handoff chain MUST
        # reuse the SAME returned BrowserProfile instance.
        ...

    def curl_impersonate_target(self, profile: BrowserProfile) -> str:
        # Map profile.chrome_major_version to the closest curl_cffi impersonate alias
        # (e.g. 'chrome131'); fall back to the generic 'chrome' only if unmatched.
        ...

    async def validate_ja4(self, driver: str, proxy: Optional[str] = None) -> str:
        # Route a test request through mitmproxy and return JA4 hash.
        # Steps:
        # 1. Start mitmproxy in subprocess.
        # 2. Make request via specified driver through mitmproxy.
        # 3. Extract JA4 from mitmproxy logs.
        # 4. Compare against expected profile.
        # Returns: JA4 hash string.
        ...
```

---

### 3.16 `ScraperRegistry` (Auto-Registration — Conflict-Free Extension Point)
**File:** `core/registry.py`
**Responsibility:** Let scrapers self-register so **no central file is edited** when one is added.
This is the seam that makes parallel agent work conflict-free (see `GitHub.md` / `CLAUDE.md`).

```python
from typing import Type

# Module-level registry. Scrapers append to it via the decorator on import — agents
# add a NEW file and decorate it; they never edit engine.py or a central dict.
_REGISTRY: dict[str, type['BaseScraper']] = {}

def register_scraper(*domains: str):
    # Class decorator: binds each domain to the scraper class at import time.
    #   @register_scraper('ft.com', 'www.ft.com')
    #   class FTScraper(PremiumScraper): ...
    def _wrap(cls: Type['BaseScraper']) -> Type['BaseScraper']:
        for d in domains:
            if d in _REGISTRY and _REGISTRY[d] is not cls:
                raise ValueError(f'duplicate scraper registration for {d}')
            _REGISTRY[d] = cls
        return cls
    return _wrap

def get_scraper_for(domain: str) -> type['BaseScraper'] | None:
    # Exact-then-suffix match; returns None so the engine can fall back to PublicScraper.
    ...

def discover_scrapers() -> None:
    # Import every module under scrapers/ (pkgutil.walk_packages) so all
    # @register_scraper decorators run. Called once at engine startup.
    ...
```

**Invariant #16 (no central registry edits):** a new scraper is *one new file + `@register_scraper`*.
Editing a shared dict to register a scraper is forbidden.

---

### 3.17 `ScrapeEngine`
**File:** `core/engine.py`

```python
class ScrapeEngine:
    # Main orchestrator. Routes URLs to correct scraper, manages retries, and coordinates auth.
    #
    # Invariants:
    # - Singleton per process.
    # - Routing is via core.registry.get_scraper_for(domain) — NOT a central dict on the engine.
    # - The engine owns NO resilience state itself: proactive politeness is delegated to RateLimiter
    #   and the reactive circuit-breaker policy to CircuitBreaker (symmetric collaborators). The engine
    #   only coordinates them — route -> gate -> run -> persist (one level of abstraction).

    def __init__(self, sink: Optional['ArticleSink'] = None) -> None:
        discover_scrapers()  # import scraper modules so @register_scraper runs
        self.proxy_rotator = ProxyRotator()
        self.auth_manager = AuthManager()
        self.fingerprint_manager = FingerprintManager()
        self.rate_limiter = RateLimiter()         # proactive per-domain politeness
        self.circuit_breaker = CircuitBreaker()   # reactive per-domain pause policy (see §3.19a)
        self.sink = sink                          # optional ArticleSink (e.g. JsonlSink)

    async def scrape(self, url: str) -> ScrapeResult:
        # Main entry point.
        # Flow:
        # 1. Parse domain from URL.
        # 2. if not self.circuit_breaker.allow(domain): short-circuit (domain is paused).
        # 3. await self.rate_limiter.acquire(domain)  (proactive politeness)
        # 4. cls = core.registry.get_scraper_for(domain).
        # 5. If None, use PublicScraper (catch-all).
        # 6. Assign proxy from rotator (await get_healthy_proxy()).
        # 7. Instantiate scraper with bridge + proxy.
        # 8. await scraper.scrape(url).
        # 9. self.circuit_breaker.record(domain, success); on success, await self.sink.write(result)
        #    if sink set.
        # 10. Return result.
        ...

    async def batch_scrape(self, urls: List[str]) -> List[ScrapeResult]:
        # Group URLs by domain, scrape each group with domain's scraper.
        ...

    # NOTE: no register_scraper() method on the engine — registration is decorator-driven
    # via core.registry.register_scraper at import time (Invariant #16). For dynamic/runtime
    # registration (rare), call core.registry.register_scraper(...) directly.
    #
    # NOTE: the circuit-breaker state and policy live in core/circuit_breaker.py (§3.19a), NOT here.
    # The engine holds a CircuitBreaker instance and calls allow()/record() — no private CB methods.
```

---

### 3.18 `ArticleSink` / `JsonlSink` / `PostgresSink` (Storage Layer)
**Package:** `core/storage/` — the seam and its backends are **separate files** so a CLI-only run never
imports the DB stack (and vice-versa). SRP: the interface, the file backend, and the DB backend are
three responsibilities at two abstraction levels.
- `core/storage/base.py` — `ArticleSink` ABC + the shared `url_id()` helper (below).
- `core/storage/jsonl.py` — `JsonlSink` (file I/O + resume manifest; local/CLI).
- `core/storage/postgres.py` — `PostgresSink` (async UPSERT via `core/db/`; serving plane).

**Responsibility:** Persist results in a RAG-ready, deduplicated, resumable way. Output is the
boundary to the downstream LLM/RAG pipeline.

```python
# core/storage/base.py
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import hashlib

def url_id(url: str) -> str:
    # Stable document id for dedup/resume.
    return hashlib.sha256(url.encode()).hexdigest()

class ArticleSink(ABC):
    # Invariants:
    # - write() is idempotent per url (content-hash dedup).
    # - seen() reflects the resume manifest so batch jobs skip completed URLs after a crash.

    @abstractmethod
    async def write(self, result: ScrapeResult) -> None: ...

    @abstractmethod
    def seen(self, url: str) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...

# core/storage/jsonl.py
class JsonlSink(ArticleSink):
    # Append-only JSONL writer (one article per line) + sidecar resume manifest.
    #
    # Layout:
    #   <output>.jsonl       -> one flattened {id, url, title, content, author,
    #                            publish_date, metadata...} per line
    #   <output>.manifest    -> newline-delimited url_id() of completed URLs
    #
    # Invariants:
    # - On init, load <output>.manifest into an in-memory set for seen().
    # - Skip writes whose sha256(normalized_content) already emitted (content dedup).
    # - Append url_id() to the manifest AFTER a successful write (crash-safe resume).

    def __init__(self, output: Path) -> None:
        self.path = output.with_suffix('.jsonl')
        self.manifest_path = output.with_suffix('.manifest')
        self._seen_urls: set[str] = self._load_manifest()
        self._seen_content: set[str] = set()

    def seen(self, url: str) -> bool:
        return url_id(url) in self._seen_urls

    async def write(self, result: ScrapeResult) -> None:
        # 1. If result.status != 'success' or no article, skip.
        # 2. content_hash = sha256(normalized content); if already emitted, skip (dedup).
        # 3. Append one JSON line (aiofiles); flush.
        # 4. Append url_id to manifest; update in-memory sets.
        ...

    def _load_manifest(self) -> set[str]:
        # Read manifest if present; return set of completed url_ids.
        ...

    async def close(self) -> None:
        ...
```

**Used by:** `cli.py` (constructs from `--output`), `ScrapeEngine`, `BaseScraper.batch_scrape`.

```python
# core/storage/postgres.py
class PostgresSink(ArticleSink):
    # Production sink for the serving plane — same ArticleSink interface as JsonlSink, so the
    # engine/scrapers stay storage-agnostic. JsonlSink remains for local/CLI runs.
    #
    # Invariants:
    # - write() UPSERTs into `articles` keyed by id = sha256(url) -> dedup is a DB constraint.
    # - seen(url) is a cheap existence check (SELECT 1 ... WHERE id = :id) -> resume.
    # - Uses the async session from core/db/session.py; never blocks the event loop.

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory   # async_sessionmaker

    async def write(self, result: ScrapeResult) -> None:
        # If status != 'success' or no article, skip. Else UPSERT the row (ON CONFLICT (id) DO UPDATE),
        # storing url/domain/bucket/title/content/author/publish_date/fetched_at/metadata.
        ...

    def seen(self, url: str) -> bool:
        # Existence check by url_id(url). (Sync signature kept for ArticleSink parity; backed by a
        # short-lived session / cached set populated at batch start.)
        ...

    async def close(self) -> None:
        ...
```

---

### 3.19 `RateLimiter`
**File:** `core/rate_limiter.py`
**Responsibility:** Proactive per-domain politeness (the circuit breaker is only reactive).

```python
import asyncio

class RateLimiter:
    # Per-domain min-interval / token-bucket gate.
    #
    # Invariants:
    # - acquire() blocks until the domain's next slot is free; FIFO per domain.
    # - Premium domains enforce a hard floor (>= PREMIUM_MIN_INTERVAL_SECONDS, e.g. 60s).
    # - Defaults come from Settings; per-domain overrides allowed.

    def __init__(self, default_interval_s: float = 1.0, overrides: Optional[dict] = None) -> None:
        self._intervals: dict[str, float] = overrides or {}
        self._default = default_interval_s
        self._next_allowed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, domain: str) -> None:
        # Await until now >= self._next_allowed[domain]; then reserve the next slot.
        ...
```

**Owned by:** `ScrapeEngine` (singleton). Called in `Engine.scrape` before dispatch.

---

### 3.19a `CircuitBreaker`
**File:** `core/circuit_breaker.py`
**Responsibility:** Reactive per-domain failure policy — pause a domain after repeated failures so the
engine stops hammering a target that is blocking us. Extracted from `ScrapeEngine` (which only
coordinates it) so the resilience policy + its state live in one place, symmetric with `RateLimiter`
(proactive). SRP: the engine routes; the breaker decides when a domain is too hot to touch.

```python
class CircuitBreaker:
    # Per-domain trip policy. Owns ALL breaker state; the engine never reaches inside it.
    #
    # Invariants:
    # - allow(domain) is False while a domain is paused; True otherwise.
    # - Trip rule: >= CIRCUIT_BREAKER_FAILURE_THRESHOLD failures within the failure window
    #   pauses the domain for CIRCUIT_BREAKER_PAUSE_MINUTES (defaults from Settings; e.g. 5 fails
    #   in 10 min -> pause 30 min).
    # - record(domain, success=True) resets the failure count; success after a pause closes the breaker.

    def __init__(self, threshold: int = 5, pause_minutes: int = 30) -> None:
        # domain -> {failures, last_failure, paused_until}
        self._state: dict[str, dict] = {}
        self._threshold = threshold
        self._pause_minutes = pause_minutes

    def allow(self, domain: str) -> bool:
        # Return True if the domain is not currently paused (and lazily clear an expired pause).
        ...

    def record(self, domain: str, success: bool) -> None:
        # On success: reset the domain's failure count. On failure: increment and trip if over threshold.
        ...
```

**Owned by:** `ScrapeEngine` (singleton). `allow()` is checked before dispatch; `record()` after.

---

### 3.20 Response Validators (Soft-Block Detection)
**File:** `utils/validators.py`
**Responsibility:** Catch HTTP-200 decoy/honeypot pages that anti-bot systems serve instead of a 403.

```python
def response_is_valid(html: str, selectors: dict, min_content_len: int = 500) -> bool:
    # Returns False (=> treat as a soft block) if ANY of:
    # - len(extracted main content) < min_content_len, OR
    # - none of selectors['content'] match the DOM, OR
    # - html matches a known block-page signature (Cloudflare/Imperva interstitial
    #   markers, "Just a moment...", "Request unsuccessful", challenge-platform scripts).
    # Scrapers raise ChallengeError on False so the normal escalation ladder triggers.
    ...
```

**Used by:** every bucket scraper (inside `_extract_article`). **Scope: response/soft-block only** —
JA4/TLS validation is NOT here; it lives on `FingerprintManager.validate_ja4()` (§3.15).

---

### 3.21 Datastore Models (`core/db/models.py`)
**Responsibility:** the shared store both planes use. SQLAlchemy 2.0 async ORM; migrations via Alembic.

```python
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector
from datetime import datetime

class Base(DeclarativeBase): ...

class Article(Base):
    __tablename__ = 'articles'
    id: Mapped[str] = mapped_column(primary_key=True)         # sha256(url) — dedup is a PK constraint
    url: Mapped[str]
    domain: Mapped[str]                                       # indexed
    bucket: Mapped[str]                                       # 'premium'|'community'|'public'
    title: Mapped[str]
    content: Mapped[str]
    author: Mapped[str | None]
    publish_date: Mapped[datetime | None]
    fetched_at: Mapped[datetime]
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)   # source provenance, driver_used, etc.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)  # Phase-2 RAG

    # Phase 2 — AI summarization (added by the summarizer worker; NULL until processed)
    relevance: Mapped[int | None] = mapped_column(index=True, nullable=True)
    """Overall AI relevance-to-owner score 1–10 (NULL until scored). Indexed for 'top by relevance' queries."""
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    """{bullets: list[str], scores: dict[str,int], reason: str, model: str, generated_at: ISO-8601}. NULL until summarized."""

class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str]                                       # queued|running|done|error
    source: Mapped[str]                                       # platform/domain or 'url-list'
    params: Mapped[dict] = mapped_column(JSONB, default=dict) # urls, bucket, limit
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None]
    result_count: Mapped[int] = mapped_column(default=0)

# (optional) Source — scheduled targets the scheduler enqueues recurringly.
```

`core/db/session.py` exposes an async engine + `async_sessionmaker` built from `DATABASE_URL`.
`core/db/repositories.py` holds the read queries the API uses (filter/paginate; never raw SQL in routes).

---

### 3.22 Serving API (`api/`)
**Responsibility:** the **read + enqueue** plane. FastAPI. **Never drives a browser** (Invariant #18).

```python
# api/app.py
def create_app() -> FastAPI:
    # Build the app: include routers (articles, jobs, search, health), add middleware
    # (API-key auth, CORS, request-id, structured logging). Returns the ASGI app.
    ...

# api/auth.py — dependency that validates X-API-Key against Settings.API_KEYS and applies a
#               per-key rate limit; raises 401 (missing/invalid) or 429 (rate limited).

# api/routes/jobs.py
@router.post('/jobs', status_code=202)
async def submit_job(body: JobIn, queue=Depends(get_queue)) -> JobOut:
    # Persist a Job(status='queued'), enqueue arq task 'run_scrape_job' with the job id, return JobOut.
    # MUST NOT call ScrapeEngine / any driver here — only enqueue.
    ...

@router.get('/articles')
async def list_articles(domain: str | None = None, bucket: str | None = None,
                        since: datetime | None = None, limit: int = 50, cursor: str | None = None,
                        session=Depends(get_session)) -> list[ArticleOut]:
    # Delegates to repositories.query_articles(...). Read-only.
    ...
```

`api/schemas.py`: `ArticleOut`, `JobIn {source, urls?, bucket?, limit?}`, `JobOut {id, status, ...}`.

---

### 3.23 Workers & Scheduler (`worker/`)
**Responsibility:** the ingestion side of the queue. `arq` (async Redis).

```python
# worker/main.py
async def run_scrape_job(ctx, job_id: str) -> None:
    # 1. Mark Job running. 2. Build ScrapeEngine(sink=PostgresSink(session_factory)).
    # 3. await engine.batch_scrape(urls)  (engine handles routing/rate-limit/escalation/sink writes).
    # 4. Mark Job done with result_count, or error with the message.

class WorkerSettings:
    functions = [run_scrape_job]
    redis_settings = ...    # from REDIS_URL

# worker/scheduler.py
class SchedulerSettings:
    cron_jobs = [...]       # arq cron → enqueue run_scrape_job per configured Source ("continuously populate")
```

---

### 3.24 LLM Summarization Port (`core/llm/`) — Phase 2

**Responsibility:** provider-agnostic article summarization. Produces a 5-bullet investor
summary and a 1–10 relevance score in a single LLM call per article. Writes results back to
the `articles` table as `summary` (JSONB) and `relevance` (INT). This is a **batch
read-modify-write over Postgres** — it is NOT on the claim-check transform path (see
Invariant #18 carve-out notes in §9).

#### `SummaryResult` (frozen dataclass, `core/llm/base.py`)

```python
@dataclass(frozen=True)
class SummaryResult:
    bullets: list[str]          # exactly 5 investor-focused bullet points
    relevance: int              # overall score 1–10 (clamped; 10 = immediately actionable)
    scores: dict[str, int]      # {"relevance":n, "credibility":n, "intensity":n, "personal":n, "time":n}
    reason: str                 # one-sentence plain-English explanation of the relevance score
    model: str                  # model name echoed from the API response
```

#### `Summarizer` (ABC, `core/llm/base.py`)

```python
class Summarizer(ABC):
    @abstractmethod
    async def summarize(
        self,
        *,
        title: str,
        content: str,
        published: str,          # ISO-8601 date string
        portfolio: list[str],    # owner's holdings (from SummarizerSettings)
        interests: list[str],    # owner's theme interests (from SummarizerSettings)
    ) -> SummaryResult: ...
```

#### `OpenAICompatibleSummarizer` (`core/llm/openai_compatible.py`)

Default adapter. Uses `httpx.AsyncClient` to POST to `{SUMMARY_API_BASE_URL}/chat/completions`
with a structured JSON prompt. Behaviour:

- Parses the model's JSON response; on parse failure, applies a lenient regex fallback.
  Unrecoverable parse failures raise `LLMParseError` and the worker skips that row.
- Clamps all sub-scores and the overall score to `[1, 10]`.
- On HTTP 429, raises `LLMRateLimitError`; the batch worker stops the run (rather than
  burning retries against a hard rate limit).
- Never logs or prints `SUMMARY_API_KEY`.

Default model: **Zhipu GLM-4.5-Flash** (free tier) via
`https://open.bigmodel.cn/api/paas/v4`. Swap to DeepSeek, Qwen, or any OpenAI-compatible
provider by changing `SUMMARY_API_*` env vars — no code change required.

#### `SummarizerSettings` (per-module fragment, `core/llm/settings.py`)

Per-module `BaseSettings` fragment — does **not** extend or modify the core `Settings` class
(Invariant #16). Reads from the same `.env` file.

| Variable | Default | Description |
|---|---|---|
| `SUMMARY_API_KEY` | `""` | API key; empty string = worker idles with a warning |
| `SUMMARY_API_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` | Any OpenAI-compatible base URL |
| `SUMMARY_API_MODEL` | `glm-4-flash` | Model name sent in every request |
| `SUMMARY_API_TIMEOUT` | `30` | Per-request timeout (seconds) |
| `SUMMARY_BATCH_SIZE` | `20` | Articles fetched per `summarize_pending()` call |
| `SUMMARY_PORTFOLIO` | `""` | Comma-separated list of owner's holdings (e.g. `TSLA,BTC`) |
| `SUMMARY_INTERESTS` | `""` | Comma-separated theme interests (e.g. `AI,energy`) |

`portfolio()` and `interests()` are helper properties that split the CSV strings into lists.

`SUMMARY_API_KEY` is **only ever read from `.env`** (gitignored). It is never committed,
never logged, and CI never calls the live API.

#### LLM Exception Hierarchy (`core/llm/exceptions.py`)

```
ScrapeForgeError
└── LLMError                   # base for all LLM failures
    ├── LLMRateLimitError      # HTTP 429; batch worker stops the current run
    └── LLMParseError          # model returned unparseable output; worker skips the row
```

#### Idempotent Migration (`core/db/migrations.py`)

Called at summarizer entry-point startup (`worker/run_summarize.py`) to self-heal existing
production databases. Uses raw `ALTER TABLE … ADD COLUMN IF NOT EXISTS` — no Alembic dependency.

```sql
ALTER TABLE articles ADD COLUMN IF NOT EXISTS relevance INTEGER;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS summary JSONB;
CREATE INDEX IF NOT EXISTS ix_articles_relevance ON articles (relevance);
```

#### Summarizer Worker (`worker/summarize_worker.py`, `worker/run_summarize.py`)

`summarize_pending(session, summarizer, settings)` — one batch cycle:
1. `SELECT … WHERE summary IS NULL LIMIT batch_size` (ordered by `fetched_at DESC`).
2. For each article: call `summarizer.summarize(…)` → `UPDATE articles SET relevance=…, summary=…`.
3. On `LLMParseError`: log warning, skip the row (set `summary = {"error": …}` to avoid
   re-querying a permanently-broken article).
4. On `LLMRateLimitError`: log warning, abort the run.

`run_summarize_worker(session_factory, summarizer, settings)` — outer loop: calls
`summarize_pending` in successive batches until no pending articles remain or a rate-limit
stops the run.

`worker/run_summarize.py` is the deployment entry point (`python -m scrapeforge.worker.run_summarize`),
mirrors the shape of `run_transform.py`, and invokes `core/db/migrations.py` at startup.

---

### 3.25 Relevance Digest — `--source postgres` Path (`digest/`) — Phase 2.5

**Responsibility:** once-daily relevance-ranked email digest.  The `digest send` CLI command
has two source paths:

- `--source sample` (default; CI-safe): renders the built-in sample data, no DB.
- `--source postgres`: queries Postgres for articles enriched by the summarizer (Phase 2),
  ranks by `relevance` score, and renders a bullets+badge+reason email.

The postgres path is entirely **read-only**; it never writes to any table.

#### `DigestSettings` (per-module fragment, `digest/settings.py`)

Per-module `BaseSettings` fragment — does **not** extend or modify the core `Settings` class
(Invariant #16). Reads from the same `.env` / repo secrets.

| Variable | Default | Description |
|---|---|---|
| `DIGEST_RELEVANCE_FLOOR` | `5` | Minimum relevance score (inclusive) to include an article |
| `DIGEST_TOP_N` | `10` | Maximum articles in the ranked digest |
| `DIGEST_WINDOW_HOURS` | `48` | Look-back window (hours from now) when querying Postgres |

#### `DigestItem` extended fields (`digest/models.py`)

Three new fields added to the `DigestItem` dataclass (all optional; absent on the legacy
keyword path):

```python
bullets:   list[str]      = field(default_factory=list)  # 5 investor-focused bullet points
relevance: int | None     = None                          # 1–10 overall score
reason:    str | None     = None                          # one-sentence relevance explanation
```

#### `DigestSection.key` extension

`DigestSection.key` is extended from `Literal["portfolio","themes","topics"]` to also
accept `"top"`. The `build_relevance_digest()` function returns a single section with
`key="top"` and a display title of `"Top Stories"`.

#### `build_relevance_digest()` (`digest/relevance.py`)

Pure function; no I/O:

```python
def build_relevance_digest(
    articles: Sequence[ArticleRow],
    *,
    min_relevance: int = 5,
    limit: int = 10,
) -> Digest:
```

Behaviour:
1. Filters `articles` to those with `relevance >= min_relevance`.
2. Sorts descending by `relevance`; uses `fetched_at` as a recency tiebreak.
3. Caps at `limit` items.
4. Wraps items in a single `DigestSection(key="top", title="Top Stories")`.
5. Returns a `Digest` containing that one section.

#### `load_ranked_articles()` / `load_ranked_articles_sync()` (`digest/postgres_source.py`)

Inlined async SQL query — **not** in `repositories.py` (seam rule: each module owns its own
queries; see Invariant #16).

```python
async def load_ranked_articles(
    db_url: str,
    *,
    window_hours: int = 48,
    top_n: int = 10,
) -> list[ArticleRow]:
    """Async: fetch top-N scored articles from the last window_hours."""

def load_ranked_articles_sync(
    db_url: str,
    *,
    window_hours: int = 48,
    top_n: int = 10,
) -> list[ArticleRow]:
    """Sync bridge — calls asyncio.run(); for use in CLI entry-points only."""
```

The query selects `id, title, url, published_at, fetched_at, relevance, summary` from
`articles` where `relevance IS NOT NULL` and `fetched_at >= NOW() - INTERVAL '<window_hours> hours'`,
ordered by `relevance DESC, fetched_at DESC`, limited to `top_n`.  The `summary` JSONB column is
parsed to extract `bullets` and `reason` fields into the returned `ArticleRow` objects.

`asyncio.run()` is allowed **only** in `load_ranked_articles_sync` (CLI entry-point bridge);
it is never called inside a running event loop.

#### Renderer behaviour (`digest/renderer.py`)

The renderer (`render_html()` / `render_text()`) branches on `item.bullets`:

- **Bullets path** (`item.bullets` non-empty): renders each bullet as an `<li>` element,
  appends a relevance badge (`_badge_html(relevance)`) showing the score, and appends
  `item.reason` as a muted one-line caption.
- **Legacy keyword path** (`item.bullets` empty): original keyword-list rendering, unchanged.

`_badge_html(relevance: int | None) -> str` — returns an inline HTML `<span>` styled as a
coloured pill: green (8–10), amber (5–7), grey (< 5 or `None`).

Empty-state copy (no items after filtering): `"No updates to show right now."`

#### Daily workflow integration

The `daily-digest.yml` GitHub Actions workflow reads the `DIGEST_SOURCE` **repo variable**
(Settings → Variables → Actions).  It defaults to `"sample"` so CI stays green without
Postgres.  Set `DIGEST_SOURCE=postgres` and add a `DATABASE_URL` repo secret to activate the
relevance-ranked digest in production (see the NOTE block at the top of the workflow file).

---

### 3.28 Per-User Email Delivery (`digest/`) — Phase 3.5

**Responsibility:** send each active user their own cosine-ranked digest email, isolated from the
owner digest path.

#### New settings (`digest/settings.py`, Phase 3.5 additions)

| Variable | Default | Description |
|---|---|---|
| `DIGEST_USER_TOP_N` | `10` | Max articles per per-user digest |
| `DIGEST_USER_WINDOW_HOURS` | `48` | Look-back window when reading `user_article_relevance` |
| `DIGEST_USER_SCORE_FLOOR` | `0.0` | Minimum cosine score to include an article |

#### New files

- **`digest/user_source.py`** — `ActiveUser` dataclass + `load_active_users` (reads
  `user_profiles WHERE email IS NOT NULL`) + `load_user_ranked_articles` (cosine-ranked join
  on `user_article_relevance`) + `load_all_sync` bridge.
- **`digest/user_digest.py`** — `build_user_digest(user, articles)`: one `"top"` section per
  user, articles in cosine order, showing shared 1–10 relevance and 5 summary bullets from
  the existing `articles.summary` JSONB column.

#### `deliver_all` (`digest/service.py`, Phase 3.5 addition)

```python
def deliver_all(
    *,
    source: str = "postgres",
    sender=None,
) -> DeliverySummary:
    ...
```

Behaviour:
- Loads all users with a non-null `email` via `load_active_users`.
- For each user, calls `load_user_ranked_articles` and `build_user_digest`.
- Skips users whose digest is empty (no qualifying articles).
- Sends via SMTP (default preview sender when `sender=None`).
- Failures for one user do not abort the remaining users (per-user failure isolation).
- Returns a `DeliverySummary` with sent/skipped/failed counts.

The existing `deliver()` function for the owner digest is **untouched**.

#### New CLI commands (`digest/cli.py`, Phase 3.5 additions)

| Command | Description |
|---|---|
| `digest preview-all` | Write per-user preview HTML for every active user (no send). |
| `digest send-all` | SMTP-send each active user their own relevance-ranked digest. |

#### Workflow (`daily-digest-users.yml`)

Manual trigger (`workflow_dispatch`) for now; the `schedule:` block is present but commented out
until the deployment is stable. Runs `digest send-all --source postgres`. Requires the same
`DATABASE_URL` and `DIGEST_SMTP_*` secrets as the owner digest.

---

### 3.29 User Sync (`hezzian` → `scraper_news`) — Phase 3.6

**Responsibility:** pull onboarded users from the separate `hezzian` Neon database and upsert
them into `scraper_news.user_profiles` so the Phase-3/3.5 embedding and digest pipeline can
consume them without a cross-database join.

#### Two-database reality

Users are managed by the Hezzian web app in a **separate** Neon database (`hezzian`), using
Clerk for authentication. The tables that live there are:

- `users` — Clerk identity: `clerk_user_id`, `email`, `deleted_at`
- `user_profiles` — onboarding answers: `user_id` (FK→`users.clerk_user_id`), `interests`
  (JSONB), `onboarding_completed` (bool)

Articles live in `scraper_news`. Postgres cannot join across databases, so a dedicated sync
job bridges the gap by reading `hezzian` read-only and writing `scraper_news.user_profiles`
(the table the Phase-3 pipeline already owns).

#### Configuration (`pipeline/sync_settings.py`)

`UserSyncSettings` is a per-module `BaseSettings` fragment — does **not** extend or modify the
core `Settings` class (Invariant #16).

| Variable | Default | Description |
|---|---|---|
| `HEZZIAN_DATABASE_URL` | `""` | asyncpg DSN for the `hezzian` Neon DB. **Empty string ⇒ sync idle-skips** (no error, no-op). |

When `HEZZIAN_DATABASE_URL` is not set, `sync-users` exits cleanly with a notice, so the
daily workflow stays green pre-rollout.

#### Field mapping (`map_to_profile` in `pipeline/user_sync.py`)

| `scraper_news.user_profiles` column | Source |
|---|---|
| `user_id` | `users.clerk_user_id` |
| `email` | `users.email` |
| `portfolio` | `interests['watch_tickers']` (list of ticker strings) |
| `sectors` | `interests['sectors']` + `interests['asset_classes']` concatenated |
| `focus` | `investor_type ; risk ; objective ; horizon ; regions` joined from matching `interests` keys |

`interests` is a JSONB column; missing keys default to empty list / empty string. The mapping
is pure Python (no SQL); it lives in `map_to_profile(row: dict) -> dict`.

#### Source query

`fetch_onboarded_users` issues a **static `text()` SELECT** (zero parameters, no f-strings)
against the `hezzian` engine:

```sql
SELECT u.clerk_user_id, u.email,
       up.interests
FROM   users u
JOIN   user_profiles up ON up.user_id = u.clerk_user_id
WHERE  u.deleted_at IS NULL
  AND  up.onboarding_completed = true
```

The use of SQLAlchemy `text()` here is the **sanctioned exception** to the ORM-only rule
(Invariant #16): the `hezzian` schema is owned by a foreign app and not mapped via ScrapeForge
ORM models, so static `text()` foreign-table reads are explicitly permitted. The query has no
dynamic/external-data parameters and passes the `tests/test_no_raw_sql.py` guard.

#### Sync logic (`pipeline/user_sync.py`)

- **`sync_users(session, rows)`** — upserts each mapped profile into `scraper_news.user_profiles`
  (`ON CONFLICT (user_id) DO UPDATE`). The pipeline is the **sole writer** of this table in
  `scraper_news`; the Hezzian app writes to its own copy in `hezzian`.
- **`run_sync_sync()`** — two-engine bridge: builds an async read engine for `hezzian` (from
  `HEZZIAN_DATABASE_URL`) and a write engine for `scraper_news` (from `DATABASE_URL`), calls
  `fetch_onboarded_users` on the read engine, then `sync_users` on the write engine. Returns
  the count of upserted rows.

#### CLI command

`pipeline sync-users` — added to the `pipeline_app` Typer sub-app (`pipeline/cli.py`).
Idles with a notice when `HEZZIAN_DATABASE_URL` is empty. Placed before `embed-profiles`
in the daily workflow so freshly synced profiles are embedded in the same run.

#### Daily workflow integration (`daily-pipeline.yml`)

The `sync-users` step runs **after `seed-owner` and before `embed-profiles`**:

```
init-db → ingest → summarize → prune → seed-owner → sync-users → embed-articles → embed-profiles → score-users
```

Requires the `HEZZIAN_DATABASE_URL` GitHub Actions secret. Until that secret is set, the step
idle-skips and the workflow stays green.

---

## 4. Humanization Utilities

### 4.1 `MousePathGenerator`
**File:** `utils/humanize.py`

```python
class MousePathGenerator:
    # Generates cubic bezier mouse paths with Perlin noise.

    def generate(self, start: tuple[int, int], end: tuple[int, int], duration_ms: int) -> List[tuple[int, int]]:
        # Generate path points.
        # Args:
        #   start: (x, y) start coordinates.
        #   end: (x, y) end coordinates.
        #   duration_ms: Total path duration in milliseconds.
        # Returns: List of (x, y, timestamp_ms) tuples.
        ...
```

### 4.2 `ScrollSimulator`
```python
class ScrollSimulator:
    # Simulates human-like scroll behavior.

    def generate_scrolls(self, page_height: int, viewport_height: int) -> List[tuple[int, int]]:
        # Generate scroll events.
        # Returns: List of (scroll_y, delay_ms) tuples.
        ...
```

### 4.3 `DelayEngine`
```python
class DelayEngine:
    # Randomized delay generation.

    @staticmethod
    def reading_pause() -> float:
        # Return delay in seconds: uniform(2.0, 6.0)
        ...

    @staticmethod
    def action_delay(min_ms: int = 500, max_ms: int = 2000) -> float:
        # Return delay in seconds between actions.
        ...

    @staticmethod
    def typing_interval(mean_ms: float = 80, std_ms: float = 20) -> float:
        # Return Gaussian-distributed keypress interval in seconds.
        ...
```

---

## 5. CLI Commands

> **Seam (Invariant #16):** the root `cli.py` is a thin composer. Each bucket/feature owns its own
> Typer **sub-app** in its package (e.g. `scrapers/community/cli.py` exposing `community_app`), and the
> root mounts registered sub-apps via discovery (`app.add_typer(sub_app, name=...)`). Adding a command =
> adding a file in your folder, not editing the root `cli.py`. The commands below are the *core* set.

### 5.1 `cli.py` — Thin Root (composer + global commands ONLY)

The root owns **only** cross-cutting/global commands and the sub-app mounting. Per-bucket commands
(`scrape_public`, `scrape_community`, `scrape_premium`, `login`) live in their bucket's sub-app file
(`scrapers/<bucket>/cli.py`) — adding/changing one is a new file in your folder, never an edit here.

```python
# cli.py — root composer
import typer

app = typer.Typer(name='scrapeforge', help='Multi-bucket anti-detection scraper')

def _mount_subapps() -> None:
    # Discover per-bucket Typer sub-apps and mount them (one app.add_typer each), e.g.:
    #   from scrapers.community.cli import community_app
    #   app.add_typer(community_app, name='community')
    # Done via discovery so features self-register their commands without editing this file.
    ...

_mount_subapps()

# --- Global commands only (not bucket-specific) ---

@app.command()
def verify_fingerprint(
    driver: str = typer.Option('curl_cffi', '--driver'),
    proxy: Optional[str] = typer.Option(None, '--proxy'),
):
    # Verify your outbound TLS fingerprint matches the claimed browser.
    ...

@app.command()
def list_sessions():
    # List all stored authenticated sessions.
    ...
```

### 5.2 Per-bucket sub-app (the pattern every bucket follows)

```python
# scrapers/community/cli.py — owned by the community bucket; root never edited to add this
import typer

community_app = typer.Typer(help='Community/foreign sites (Bucket 2)')

@community_app.command('scrape')
def scrape_community(
    platform: str = typer.Argument(..., help='reddit | substack | wso'),
    target: str = typer.Argument(..., help='subreddit, newsletter domain, or WSO forum URL'),
    limit: int = typer.Option(100, '--limit', '-l'),
    output: Path = typer.Option(Path('./output'), '--output', '-o'),
):
    # Scrape community/foreign sites (Bucket 2).
    ...

# premium (scrape_premium + login) and public (scrape_public) follow the same shape in
# scrapers/premium/cli.py and scrapers/public/cli.py respectively.
```

---

## 6. Extension Patterns (How to Add New Scrapers)

> **Conflict-free rule (Invariant #16):** adding a scraper is **one new file + `@register_scraper`**.
> You do NOT edit `core/engine.py`, a central `DOMAIN_REGISTRY`, `cli.py`, or a shared `Settings` class.
> This is what lets parallel agents avoid merge conflicts (see `CLAUDE.md` / `GitHub.md`).

### 6.1 Adding a New Premium Site

```python
# Create ONE new file, e.g. scrapers/premium/nytimes.py — no other file is touched.
from core.registry import register_scraper

@register_scraper('newsite.com', 'www.newsite.com')   # self-registers on import
class NewSiteScraper(PremiumScraper):
    DOMAINS = ['newsite.com', 'www.newsite.com']

    def _get_selectors(self) -> dict:
        return {
            'title': 'h1.article-headline',
            'content': 'div.article-body',
            'author': 'span.author-name',
            'publish_date': 'time[datetime]',
        }

    async def scrape(self, url: str) -> ScrapeResult:
        # Optional: override for site-specific challenge handling
        return await super().scrape(url)
# That's it — discover_scrapers() imports this module at startup so the decorator runs.
```

### 6.2 Adding a New Community Platform

```python
from core.registry import register_scraper

@register_scraper('newplatform.com')
class NewPlatformScraper(CommunityScraper):
    DOMAINS = ['newplatform.com']

    async def scrape(self, url: str) -> ScrapeResult:
        # Platform-specific logic
        ...
```

### 6.3 Adding a New Driver Backend

```python
# 1. Inherit BaseDriver
class NewDriver(BaseDriver):
    async def launch(self): ...
    async def navigate(self, url, wait_for, wait_until):
        ...
    # Implement all abstract methods (all async)

# 2. Register in StealthBridge._init_backend()
if self.driver == 'newdriver':
    return NewDriver(self.proxy, self.profile, self.timeout_ms)
```

---

## 7. Dependency Injection & Object Lifecycle

```
ScrapeEngine (singleton)
|-- created once at startup; calls core.registry.discover_scrapers() (imports scraper modules)
|-- routes via core.registry.get_scraper_for(domain)  [no central dict]
|-- owns ProxyRotator (singleton)
|-- owns RateLimiter (singleton)
|-- owns AuthManager (singleton)
|   |-- owns StateStore (singleton)
|   |-- owns SSOHandler (stateless, created per login)
|-- owns FingerprintManager (singleton)
|-- owns ArticleSink (optional singleton; JsonlSink holds the resume manifest)
|-- creates BaseScraper instances per request
    |-- single scrape: one StealthBridge (one proxy)
    |-- batch_scrape: a POOL of up to max_concurrency bridges, each its own proxy
        |-- each StealthBridge launches one BaseDriver (async with) per request
            |-- all drivers in a handoff chain share ONE BrowserProfile + ONE proxy (Invariant #11)
            |-- driver holds session/cookies for its lifetime
```

**Rule:** Scrapers and Bridges are ephemeral (per-request or per-batch). Rotators, Managers, Stores,
RateLimiter, and the Sink are singletons.

---

## 8. Configuration Schema

> **Seam (Invariant #16):** this `Settings` class holds only **core/shared** keys (rarely edited).
> Each feature owns its **own** settings fragment in its module — e.g. `RedditSettings(BaseSettings)`
> in `scrapers/community/reddit.py` reading the same `.env` — instead of everyone appending to one
> god-`Settings`. That keeps `config/settings.py` off the merge-conflict hot path.

```python
class Settings(BaseSettings):
    # Drivers
    PATCHRIGHT_CHANNEL: str = 'chrome'           # MUST be 'chrome', never 'chromium'
    CURL_CFFI_IMPERSONATE: str = 'chrome'        # Generic alias
    PRIMP_IMPERSONATE_OS: str = 'windows'        # windows | macos | linux

    # Auth
    STATE_STORE_KEY: str                           # 32+ char Fernet key
    STATE_STORE_PATH: Path = Path.home() / '.scrapeforge' / 'states'
    SESSION_TTL_DAYS: int = 7

    # Proxies
    PROXY_LIST_PATH: Path = Path('./proxies.txt')
    PROXY_ROTATION_POLICY: Literal['per_context', 'per_request', 'off'] = 'per_context'
    PROXY_HEALTH_CHECK_URL: str = 'https://httpbin.org/ip'
    PROXY_BURNED_COOLDOWN_MINUTES: int = 60

    # Humanization
    HUMANIZE_MIN_DELAY: float = 2.0
    HUMANIZE_MAX_DELAY: float = 8.0
    HUMANIZE_MOUSE_SPEED_MEAN: float = 500.0       # pixels/sec
    HUMANIZE_TYPING_MEAN_MS: float = 80.0
    HUMANIZE_TYPING_STD_MS: float = 20.0

    # NOTE: bucket-specific keys (REDDIT_*, SUBSTACK_*, and each bucket's *_MAX_CONCURRENCY) are
    # DELIBERATELY NOT here. A key used by exactly one feature belongs in that feature's own
    # settings fragment (see below), not in this shared class — keeping config/settings.py off the
    # merge-conflict hot path (Invariant #16). Only genuinely shared keys live in Settings.

    # Circuit breaker
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_PAUSE_MINUTES: int = 30

    # Rate limiting (proactive politeness)
    DEFAULT_RATE_INTERVAL_SECONDS: float = 1.0
    PREMIUM_MIN_INTERVAL_SECONDS: float = 60.0   # never batch premium

    # Parsing / extraction
    HTML_PARSER: Literal['selectolax', 'lxml'] = 'selectolax'  # decided up front; lxml fallback
    MIN_CONTENT_LENGTH: int = 500                # soft-block floor for response_is_valid()

    # Storage / output
    OUTPUT_FORMAT: Literal['jsonl'] = 'jsonl'    # RAG-ready sink
    DEFAULT_OUTPUT_PATH: Path = Path('./output')

    # Logging
    LOG_LEVEL: str = 'INFO'
    LOG_FORMAT: Literal['text', 'json'] = 'text'

    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')
```

**Per-module fragment (the pattern for every feature-specific key).** Each feature defines its own
`BaseSettings` fragment in its own module, reading the same `.env`. Adding a feature's config is a new
class in your folder — never an edit to the shared `Settings` above.

```python
# scrapers/community/reddit.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class RedditSettings(BaseSettings):
    REDDIT_USE_JSON_API: bool = True
    REDDIT_JSON_LIMIT: int = 100
    COMMUNITY_MAX_CONCURRENCY: int = 5            # this bucket's concurrency floor
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

# Likewise: SubstackSettings (SUBSTACK_USE_CURL_CFFI) in scrapers/community/substack.py,
# PremiumSettings (PREMIUM_MAX_CONCURRENCY) in scrapers/premium/_base.py,
# PublicSettings (PUBLIC_MAX_CONCURRENCY) in scrapers/public/public.py.
```

---

## 9. Invariants & Non-Negotiables

1. **No vanilla Playwright.** Always `patchright` or `nodriver`.
2. **No `playwright-stealth` or `puppeteer-extra-plugin-stealth`.** These are saturated.
3. **No raw `requests` or `httpx`.** Always `curl_cffi` or `primp`.
4. **No fixed `time.sleep()`.** Always use `DelayEngine`.
5. **No plaintext credentials or cookies.** Always Fernet-encrypted.
6. **No scripting SSO password entry in headless mode.** Always interactive login for Bucket 1.
7. **No mid-session proxy rotation.** One context = one proxy.
8. **No stale pinned browser versions.** Use aliases aligned to the *installed* Chrome major version (`FingerprintManager.curl_impersonate_target`), not a hand-pinned old one.
9. **No AGPL contamination.** `nodriver` must be isolated if commercial.
10. **No CAPTCHA solving services.** Out of scope.
11. **Fingerprint coherence across a handoff chain.** All drivers that participate in one logical session (browser solves → curl_cffi bulk-fetches) MUST share one `BrowserProfile` and one proxy IP, and the curl_cffi impersonate target MUST match the Chrome major version that obtained any `cf_clearance`. A mismatch invalidates the clearance.
12. **Fully async I/O.** Every `BaseDriver`/`StealthBridge`/`BaseScraper`/`ScrapeEngine` I/O method is `async def`. Never call `asyncio.run()` inside the running loop; wrap sync-only libs (`primp`) in `asyncio.to_thread`.
13. **Bounded escalation, explicit terminal failure.** The escalation ladder is finite (HTTP → patchright → nodriver). When the last rung fails a challenge, raise `ChallengeError` to the caller (public/community) or prompt interactive re-login (premium). No silent infinite escalation.
14. **All successful results persist via `ArticleSink`.** No ad-hoc file writes; dedup + resume go through the sink so batch jobs are crash-safe.
15. **Soft blocks are failures.** A 200 response that fails `validators.response_is_valid()` is treated as a `ChallengeError`, never reported as success.
16. **No central registry edits (extension by addition).** Scrapers self-register via `@register_scraper(...)` from `core/registry.py`. Adding a scraper is one new file; editing a shared dict/`engine.py` to register is forbidden. Same principle for CLI (per-bucket Typer sub-apps) and config (per-module settings fragments) — agents **add files, never edit shared seam files**. The only sanctioned shared file is `pyproject.toml` (deps), routed through the lead.
17. **One feature = one folder/branch/worktree.** A teammate works only inside its assigned package and never edits another feature's files (enforced socially by `CLAUDE.md`, mechanically by CODEOWNERS — see `GitHub.md`).
18. **Event-driven pipeline; storage split by purpose (Phase 6).** Ingestion and serving are decoupled
    stages over durable queues (Redis behind the `MessageQueue` port; SQS/Cloud Tasks later) — NOT a direct
    scraper→Postgres write. The flow is:
    - **API** (`api/`) is **read + enqueue only** — it MUST never drive a browser or call `ScrapeEngine`/a
      driver/a worker. `POST /jobs` persists a queued `Job` and **publishes to the job queue**; it serves
      stored rows. All data endpoints require `X-API-Key` auth. (AST-enforced in tests.)
    - **Scraper worker** (`worker/scraper_worker.py`) is **stateless w.r.t. the serving DB**: it fetches
      (curl_cffi, minimal parse), writes the RAW payload to **object storage** (claim-check, immutable, via
      the `ObjectStore` port — MinIO/S3), and **publishes a small pointer** to the results queue. It NEVER
      writes Postgres.
    - **Transform worker** (`worker/transform_worker.py`) is the **sole writer of structured data + Job
      status**: it reads raw from object storage, validates/cleans/normalizes, and **idempotently UPSERTs**
      into Postgres (dedup by PK = `sha256(url)`). Poison messages → DLQ after `QUEUE_MAX_RETRIES`.
    - **Storage is split by purpose:** raw → object store (cheap, immutable, replayable); structured →
      Postgres behind the `ArticleSink` seam (`JsonlSink` local CLI, `PostgresSink` server). Embeddings
      (pgvector) are generated in the transform layer only when semantic search is enabled.
    Rationale: raw is the expensive artifact (a residential-proxy request + block risk), so it is made
    durable the instant it is fetched — enabling reprocessing without re-scraping.

    **Community/JSON scrapers carve-out (Phase 1).** The scraper→transform claim-check split
    (stateless scraper writes raw + publishes a pointer; the transform worker is the sole
    structured writer) governs **public-bucket HTML**. Fully-parsing community scrapers (Substack;
    later Reddit) persist structured rows **within their ingestion worker**
    (`worker/community_ingest_worker.py`) via `PostgresSink`, while still archiving raw to the
    object store for claim-check/replay. Rationale: these scrapers produce complete `Article`s at
    fetch time, so a separate HTML-selector transform stage adds nothing and cannot parse their
    JSON-sourced fields. The scheduler routes such sources to the `INGEST` queue.

    **Summarizer carve-out (Phase 2).** The `summarize_worker.py` / `run_summarize.py`
    worker is an independent **batch read-modify-write over Postgres** — it is not part of the
    scraper→transform pipeline at all. It polls `articles WHERE summary IS NULL`, calls an
    external LLM (via the `core/llm/` port; see §3.24), and UPSERTs `relevance` +
    `summary` on the same row. This is the same class of carve-out as community ingestion: the
    pipeline decoupling governs the *ingest* path only. Post-ingest enrichment workers that
    read from and write back to Postgres are explicitly permitted.

    **Relevance digest carve-out (Phase 2.5).** `digest/postgres_source.py` is a read-only
    async query over Postgres (inlined SQL — not in `repositories.py`); it is consumed by the
    once-daily `digest send` CLI command, not by any pipeline worker. See §3.25.

    **Lean Render-cron carve-out (Phase 6.5 — `pipeline/`).** For lean deployments on
    serverless infrastructure (Render Cron Jobs + Neon Postgres), ScrapeForge supports a
    run-once job path that skips Redis and MinIO entirely. Three Render Cron Jobs replace the
    always-on docker-compose stack: `init-db` (deploy hook), `ingest` (hourly/daily), and
    `summarize` (daily). See §3.26.

    **Multi-user relevance carve-out (Phase 3).** `pipeline/embeddings_jobs.py`
    (`embed_articles`, `embed_profiles`, `score_users`, `seed_owner`) is the same class of
    post-ingest enrichment as the summarizer: an independent batch read-modify-write over
    Postgres, not part of the scraper→transform pipeline. It fills `articles.embedding`
    (WHERE NULL), embeds changed profiles, and writes per-user cosine scores. See Invariant #19
    and §3.27.

19. **Multi-user relevance is embeddings + pgvector, never per-user LLM (Phase 3).** Per-user
    ranking is computed by cosine similarity (`Article.embedding.cosine_distance(...)`) over the
    SHARED corpus — one embedding per article, one per changed profile, then in-database vector
    math — never a per-user LLM call. The Hezzian app is the **sole writer** of `user_profiles`;
    the pipeline only reads it and is the **sole writer** of `user_profile_vectors` and
    `user_article_relevance` (FK→`articles.id` ON DELETE CASCADE, so `prune` cleans scores).
    `EMBED_DIM` MUST equal the `Vector(N)` column width (1536). Embedding jobs idle when
    `EMBED_API_KEY` is empty, so the workflow stays green pre-rollout. The Embedder provider is
    swapped by addition behind the `core/embeddings/` port (`EMBED_PROVIDER`: `gemini` default,
    `openai_compatible` fallback) — no core edits.

---

## 3.26 Pipeline Sub-App (`scrapeforge/pipeline/`)

The `pipeline` package provides a lightweight **run-once-per-invocation** alternative to the
always-on docker-compose worker stack. It is designed for lean deployments on Render Cron Jobs
backed by Neon Postgres — no Redis, no MinIO, no persistent worker process.

### Package contents

| File | Role |
|------|------|
| `pipeline/__init__.py` | Package marker |
| `pipeline/jobs.py` | Pure-async job functions (`init_db`, `ingest_publications`); injected with fakes in tests |
| `pipeline/retention.py` | Storage retention (`prune_articles`); oldest-first deletion under the DB size cap |
| `pipeline/embeddings_jobs.py` | Phase-3 multi-user jobs (`embed_articles`, `embed_profiles`, `score_users`, `seed_owner`); pure-async, injected `Embedder` (§3.27) |
| `pipeline/cli.py` | Typer sub-app `pipeline_app` mounted in root `cli.py`; the only file that calls `asyncio.run()` (sanctioned CLI entry-point, Invariant #12) |

### Commands (`scrapeforge pipeline <cmd>`)

| Command | Purpose | Render trigger |
|---------|---------|----------------|
| `init-db` | `CREATE EXTENSION IF NOT EXISTS vector` + `Base.metadata.create_all` + `ensure_summary_columns` — idempotent, re-runable | One-off "Deploy Job" |
| `ingest [--limit N] [--sector S] [--max M]` | Scrape curated Substacks → UPSERT into Postgres via `PostgresSink` (no queue, no object store) | Hourly/daily Cron Job |
| `summarize` | Drain `articles WHERE summary IS NULL` → LLM → UPDATE; exits when done | Daily Cron Job (after ingest) |
| `prune` | Delete old / low-relevance articles oldest-first to stay under the DB cap | Daily Cron Job |
| `seed-owner` | Upsert `user_id='owner'` profile from `SUMMARY_PORTFOLIO`/`INTERESTS`/`FOCUS` | Daily (before embed) |
| `embed-articles` | Fill `articles.embedding` WHERE NULL via the `Embedder` port; idle if no `EMBED_API_KEY` | Daily (after summarize) |
| `embed-profiles` | Embed changed `user_profiles` (source-hash gate) → `user_profile_vectors` | Daily (after embed-articles) |
| `score-users` | pgvector cosine top-K per user → `user_article_relevance`; no LLM, no API key | Daily (after embed-profiles) |

### How it differs from the event-driven worker stack

| Concern | docker-compose (always-on) | Render Cron (run-once) |
|---------|--------------------------|----------------------|
| Queue | Redis `MessageQueue` | None — direct function call |
| Object store | MinIO `ObjectStore` (claim-check) | None — UPSERT directly |
| Process model | Long-running worker drains queue | One-shot: run, drain, exit |
| Infra | VPS + Docker Compose | Render Cron + Neon |
| Resumability | Queue + DLQ | Idempotent UPSERT (sha256 dedup) |

The run-once jobs call the same `PostgresSink`, `SubstackScraper`, and `OpenAICompatibleSummarizer`
that the full pipeline uses — only the transport layer (queue vs. direct call) differs.

## 3.27 Embedder Port (`scrapeforge/core/embeddings/`)

The Phase-3 multi-user relevance plane (Invariant #19). Mirrors the `core/llm/` Summarizer port:
a provider-agnostic boundary the embedding jobs depend on, swapped by addition — no core edits.

| File | Role |
|------|------|
| `core/embeddings/base.py` | `Embedder` ABC — `async def embed(texts: list[str]) -> list[list[float]]` |
| `core/embeddings/exceptions.py` | `EmbeddingError` / `EmbeddingRateLimitError` / `EmbeddingParseError` (under `ScrapeForgeError`) |
| `core/embeddings/settings.py` | `EmbedderSettings` fragment (`EMBED_*`); `EMBED_DIM` defaults 1536 (= the `Vector(1536)` columns) |
| `core/embeddings/gemini.py` | `GeminiEmbedder` (PRIMARY) — Gemini `:batchEmbedContents` over httpx; key in `x-goog-api-key` header |
| `core/embeddings/openai_compatible.py` | `OpenAICompatibleEmbedder` (fallback, e.g. Jina) — POSTs `{BASE}/embeddings` |
| `core/embeddings/factory.py` | `make_embedder(settings)` — picks the adapter by `EMBED_PROVIDER` |

**Data contract (shared Postgres).** App-owned `user_profiles(user_id, email, portfolio[],
sectors[], focus, updated_at)`; pipeline-owned `user_profile_vectors(user_id, embedding,
source_hash, updated_at)` and `user_article_relevance(user_id, article_id→articles.id CASCADE,
score, computed_at)`. `articles.embedding Vector(1536)` is filled by `embed-articles`. The app
reads `user_article_relevance` (indexed `(user_id, score)`) to build each user's feed.

**`user_profiles` column added in Phase 3.5:**
- `email text NULL` — the Hezzian app writes the user's email (from Clerk) at signup; the
  pipeline reads it to address per-user digests. `NULL` ⇒ the user is skipped at send time.

### `DATABASE_SSL` — opt-in TLS for Neon

`core/db/session.py::make_engine` calls `_ssl_connect_args()` which returns
`{"ssl": True}` when `DATABASE_SSL` is set to `require`, `true`, or `1` (case-insensitive).
When unset (local dev, CI), SSL is off. On Render, set `DATABASE_SSL=require` in the service
environment to satisfy Neon's TLS requirement.

```
DATABASE_SSL=require   # Render + Neon (serverless Postgres requires TLS)
# (unset)             # local dev / CI — plain connection
```

No code change is needed to switch between plain and TLS — the env var is the only knob.