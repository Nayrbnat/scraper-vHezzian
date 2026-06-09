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
        # Helper: parse HTML into Article. Uses selectolax (lxml fallback).
        # Subclasses override _get_selectors() for domain-specific CSS.
        # Raises ChallengeError if validators.response_is_valid() fails (soft-block).
        ...

    def _get_selectors(self) -> dict:
        # Return CSS selectors for this domain.
        # Expected keys: title, content, author, publish_date
        return {}
```

---

### 3.8 `PremiumScraper` (Bucket 1 Base)
**File:** `scrapers/bucket1_premium.py`

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
**File:** `scrapers/bucket2_community.py`

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
**File:** `scrapers/bucket3_public.py`

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
    # - Circuit breaker: if a domain fails 5 times in 10 min, pause for 30 min.

    def __init__(self, sink: Optional['ArticleSink'] = None) -> None:
        discover_scrapers()  # import scraper modules so @register_scraper runs
        self.proxy_rotator = ProxyRotator()
        self.auth_manager = AuthManager()
        self.fingerprint_manager = FingerprintManager()
        self.rate_limiter = RateLimiter()         # proactive per-domain politeness
        self.sink = sink                          # optional ArticleSink (e.g. JsonlSink)
        self._circuit_breakers: dict[str, dict] = {}  # domain -> {failures, last_failure, paused_until}

    async def scrape(self, url: str) -> ScrapeResult:
        # Main entry point.
        # Flow:
        # 1. Parse domain from URL.
        # 2. Check circuit breaker (reactive).
        # 3. await self.rate_limiter.acquire(domain)  (proactive politeness)
        # 4. cls = core.registry.get_scraper_for(domain).
        # 5. If None, use PublicScraper (catch-all).
        # 6. Assign proxy from rotator (await get_healthy_proxy()).
        # 7. Instantiate scraper with bridge + proxy.
        # 8. await scraper.scrape(url).
        # 9. Update circuit breaker on failure; on success, await self.sink.write(result) if sink set.
        # 10. Return result.
        ...

    async def batch_scrape(self, urls: List[str]) -> List[ScrapeResult]:
        # Group URLs by domain, scrape each group with domain's scraper.
        ...

    # NOTE: no register_scraper() method on the engine — registration is decorator-driven
    # via core.registry.register_scraper at import time (Invariant #16). For dynamic/runtime
    # registration (rare), call core.registry.register_scraper(...) directly.

    def _check_circuit_breaker(self, domain: str) -> bool:
        # Return True if domain is currently paused.
        ...

    def _update_circuit_breaker(self, domain: str, success: bool) -> None:
        # Increment failure count or reset on success.
        ...
```

---

### 3.18 `ArticleSink` / `JsonlSink` (Storage Layer)
**File:** `core/storage.py`
**Responsibility:** Persist results in a RAG-ready, deduplicated, resumable way. Output is the
boundary to the downstream LLM/RAG pipeline.

```python
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

**Used by:** every bucket scraper (after `_extract_article`), `FingerprintManager` tests.

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

### 5.1 `cli.py` — Typer App Structure

```python
import typer

app = typer.Typer(name='scrapeforge', help='Multi-bucket anti-detection scraper')
# Per-feature sub-apps are auto-mounted at startup (one app.add_typer per registered bucket);
# features add their own sub-app file rather than editing this module.

@app.command()
def scrape_public(
    source: str = typer.Argument(..., help='URL or domain to scrape'),
    output: Path = typer.Option(Path('./output'), '--output', '-o'),
    limit: int = typer.Option(100, '--limit', '-l'),
    proxy: Optional[str] = typer.Option(None, '--proxy'),
):
    # Scrape public news outlets (Bucket 3).
    ...

@app.command()
def scrape_community(
    platform: str = typer.Argument(..., help='reddit | substack | wso'),
    target: str = typer.Argument(..., help='subreddit name, newsletter domain, or WSO forum URL'),
    limit: int = typer.Option(100, '--limit', '-l'),
    output: Path = typer.Option(Path('./output'), '--output', '-o'),
):
    # Scrape community/foreign sites (Bucket 2).
    ...

@app.command()
def login(
    site: str = typer.Argument(..., help='Domain to log into (e.g., ft.com)'),
    interactive: bool = typer.Option(True, '--interactive/--headless'),
    vnc: bool = typer.Option(False, '--vnc', help='Use Camoufox VNC server'),
):
    # Interactive login for premium sites (Bucket 1).
    ...

@app.command()
def scrape_premium(
    site: str = typer.Argument(..., help='Domain (e.g., ft.com)'),
    url: Optional[str] = typer.Option(None, '--url'),
    batch_file: Optional[Path] = typer.Option(None, '--batch-file'),
    output: Path = typer.Option(Path('./output'), '--output', '-o'),
):
    # Scrape premium paywalled articles using stored session.
    ...

@app.command()
def verify_fingerprint(
    driver: str = typer.Option('curl_cffi', '--driver'),
    proxy: Optional[str] = typer.Option(None, '--proxy'),
):
    # Verify your outbound TLS fingerprint matches claimed browser.
    ...

@app.command()
def list_sessions():
    # List all stored authenticated sessions.
    ...
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

    # Bucket-specific
    REDDIT_USE_JSON_API: bool = True
    REDDIT_JSON_LIMIT: int = 100
    SUBSTACK_USE_CURL_CFFI: bool = True
    PUBLIC_MAX_CONCURRENCY: int = 5
    PREMIUM_MAX_CONCURRENCY: int = 1
    COMMUNITY_MAX_CONCURRENCY: int = 5

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
18. **Decoupled planes.** Ingestion (browsers/proxies, in `worker/`) and serving (`api/`) are separate processes over one Postgres. The **API is read + enqueue only and MUST never drive a browser or call `ScrapeEngine` directly** — it pushes a job to Redis; a worker runs the engine and writes via `PostgresSink`. All data endpoints require `X-API-Key` auth. Storage stays behind the `ArticleSink` seam (`JsonlSink` local, `PostgresSink` server) so the engine is store-agnostic.