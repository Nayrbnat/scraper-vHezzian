"""StealthBridge — unified async driver interface (SPEC.md §3.1).

Every scraper talks to the bridge, never directly to Playwright / curl_cffi /
primp / nodriver.  The bridge owns the driver lifecycle:

- ``_init_backend()`` maps the *driver* string to a backend instance (config
  only — no I/O).
- ``__aenter__`` launches the backend (async I/O).
- ``__aexit__`` and ``close()`` clean up deterministically.

Invariants
----------
- One ``StealthBridge`` instance = one proxy session (session affinity,
  Invariant #7).
- Driver selection is immutable after construction.
- ``close()`` is idempotent.
- No I/O in ``__init__`` or ``_init_backend``.
"""

from __future__ import annotations

from typing import Literal

from scrapeforge.core.drivers.base import BaseDriver
from scrapeforge.core.drivers.curl_cffi_driver import CurlCffiDriver
from scrapeforge.core.fingerprint_manager import FingerprintManager
from scrapeforge.core.models import BrowserProfile, ScrapeResult, StorageState
from scrapeforge.exceptions import DriverError


class StealthBridge:
    """Unified interface over all automation driver backends.

    Usage::

        async with StealthBridge('curl_cffi', proxy='http://...', profile=p) as bridge:
            result = await bridge.navigate('https://example.com')
            html = await bridge.get_html()
    """

    def __init__(
        self,
        driver: Literal["curl_cffi", "primp", "patchright", "nodriver"],
        proxy: str | None = None,
        profile: BrowserProfile | None = None,
        headless: bool = True,
        timeout_ms: int = 30000,
    ) -> None:
        self.driver = driver
        self.proxy = proxy
        # Use supplied profile or generate from installed Chrome.
        # In unit tests always supply an explicit profile to avoid probing the host.
        self.profile: BrowserProfile = profile or FingerprintManager().generate_profile("chrome")
        self.headless = headless
        self.timeout_ms = timeout_ms
        # Backend is constructed (config only) here; its session is launched in __aenter__.
        self._backend: BaseDriver = self._init_backend()
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Backend factory (no I/O)
    # ------------------------------------------------------------------

    def _init_backend(self) -> BaseDriver:
        """Map *self.driver* to the matching ``BaseDriver`` subclass.

        Phase-2 drivers (primp, patchright, nodriver) are not available yet and
        raise ``DriverError`` at construction time.
        """
        if self.driver == "curl_cffi":
            return CurlCffiDriver(self.proxy, self.profile, self.timeout_ms)
        if self.driver in ("primp", "patchright", "nodriver"):
            raise DriverError(f"driver {self.driver!r} not available until Phase 2+")
        raise DriverError(f"unknown driver {self.driver!r}")

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> StealthBridge:
        """Launch the backend; required before ``navigate()``."""
        await self._backend.launch()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(
        self,
        url: str,
        wait_for: str | None = None,
        wait_until: Literal["load", "domcontentloaded", "networkidle"] = "domcontentloaded",
    ) -> ScrapeResult:
        """Navigate to *url* and return a ``ScrapeResult``.

        Delegates to the backend; see ``BaseDriver.navigate`` for error contract.
        """
        return await self._backend.navigate(url, wait_for, wait_until)

    # ------------------------------------------------------------------
    # Content accessors
    # ------------------------------------------------------------------

    async def get_html(self) -> str:
        """Return the current page HTML."""
        return await self._backend.get_html()

    async def get_text(self, selector: str) -> str | None:
        """Extract text from the first element matching *selector*."""
        return await self._backend.get_text(selector)

    # ------------------------------------------------------------------
    # Challenge / anti-bot
    # ------------------------------------------------------------------

    async def solve_challenge(self) -> bool:
        """Auto-detect and solve a Cloudflare Turnstile / JS challenge.

        HTTP-only backends always return ``False``; browser backends attempt
        human-like interaction.
        """
        return await self._backend.solve_challenge()

    # ------------------------------------------------------------------
    # Cookie / session management
    # ------------------------------------------------------------------

    async def export_cookies(self) -> list[dict]:
        """Export cookies in Netscape-compatible format."""
        return await self._backend.export_cookies()

    async def import_cookies(self, cookies: list[dict]) -> None:
        """Import *cookies* into the current session."""
        await self._backend.import_cookies(cookies)

    async def inject_storage_state(self, state: StorageState) -> None:
        """Inject cookies + Web Storage from *state* into the current session."""
        await self._backend.inject_storage_state(state)

    async def export_storage_state(self) -> StorageState:
        """Export the current session state (cookies, localStorage, sessionStorage)."""
        return await self._backend.export_storage_state()

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def screenshot(self, path: str | None = None) -> bytes:
        """Capture a PNG screenshot.  Raises ``DriverError`` for HTTP-only drivers."""
        return await self._backend.screenshot(path)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the backend session / browser.  Idempotent — safe to call twice."""
        if not self._closed:
            await self._backend.close()
            self._closed = True
