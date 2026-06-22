"""CurlCffiDriver — HTTP-only TLS-impersonating driver (SPEC.md §3.3).

Uses ``curl_cffi.requests.AsyncSession`` to impersonate Chrome's TLS
fingerprint (JA4 / HTTP/2 settings) without running a real browser.

Invariants
----------
- No JavaScript execution.  ``solve_challenge()`` always returns ``False``.
- The impersonate target is *derived* from ``profile.chrome_major_version``
  via ``FingerprintManager.curl_impersonate_target`` — never a stale pin
  (Invariants #8, #11).
- Session persists cookies across requests within the same instance.
- All I/O is async; no blocking calls on the event loop.
"""

from __future__ import annotations

import time

from curl_cffi.requests import AsyncSession

from scrapeforge.core.drivers.base import BaseDriver
from scrapeforge.core.fingerprint_manager import FingerprintManager
from scrapeforge.core.models import BrowserProfile, ScrapeResult, StorageState
from scrapeforge.exceptions import ChallengeError, DriverError, RateLimitError


class CurlCffiDriver(BaseDriver):
    """HTTP driver using curl_cffi for TLS/HTTP2 Chrome impersonation."""

    def __init__(
        self,
        proxy: str | None,
        profile: BrowserProfile,
        timeout_ms: int,
    ) -> None:
        super().__init__(proxy, profile, timeout_ms)
        self._impersonate: str = FingerprintManager().curl_impersonate_target(profile)
        self._session: AsyncSession | None = None
        self._last_response = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def launch(self) -> None:
        """Create the async session bound to proxy and impersonate target.

        Also applies the ``User-Agent`` and ``Accept-Language`` from the profile
        as default headers so every subsequent request carries them.
        """
        self._session = AsyncSession(
            impersonate=self._impersonate,
            proxy=self.proxy,
        )
        # Apply profile headers to every request in this session.
        self._session.headers.update(
            {
                "User-Agent": self.profile.user_agent,
                "Accept-Language": self.profile.accept_language,
            }
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(
        self,
        url: str,
        wait_for: str | None = None,
        wait_until: str = "domcontentloaded",
    ) -> ScrapeResult:
        """Perform an async GET request and return a ``ScrapeResult``.

        Challenge / error detection (in order):
        - ``status == 403`` AND ``'cf-mitigated'`` header present (case-insensitive key
          match) → ``ChallengeError``
        - ``status == 403`` AND ``'cloudflare'`` in first 4096 chars of body
          (case-insensitive) → ``ChallengeError``
        - ``status == 403`` with neither marker → ``DriverError("HTTP 403")``
        - ``status == 429`` → ``RateLimitError``
        - ``status >= 400`` (other)  → ``DriverError``
        - ``status == 200`` → ``ScrapeResult(status='success', ...)``

        The driver does NOT extract an ``Article``; the scraper layer does that
        via ``get_html()`` and the parser utilities.
        """
        t0 = time.monotonic()
        resp = await self._session.get(url, timeout=self.timeout_ms / 1000)  # type: ignore[union-attr]
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        self._last_response = resp
        status = resp.status_code

        if status == 403:
            # Build a lowercased key view so detection is case-insensitive regardless
            # of how the HTTP stack normalises header names (e.g. 'Cf-Mitigated' vs
            # 'cf-mitigated').
            lower_headers = {k.lower(): v for k, v in resp.headers.items()}
            if "cf-mitigated" in lower_headers:
                raise ChallengeError(
                    f"Cloudflare challenge detected (cf-mitigated header) for {url}"
                )
            # Bound the body scan to the first 4096 chars — the interstitial marker
            # appears early and this avoids decoding/copying a potentially large body.
            if "cloudflare" in resp.text[:4096].lower():
                raise ChallengeError(f"Cloudflare challenge detected (body match) for {url}")

        if status == 429:
            raise RateLimitError(f"HTTP 429 — rate limited by {url}")

        if status >= 400:
            raise DriverError(f"HTTP {status} for {url}")

        return ScrapeResult(
            status="success",
            driver_used="curl_cffi",
            proxy_used=self.proxy,
            fetch_duration_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # Content accessors
    # ------------------------------------------------------------------

    async def get_html(self) -> str:
        """Return the raw HTML of the last response.

        Raises
        ------
        DriverError
            When called before any successful ``navigate()``.
        """
        if self._last_response is None:
            raise DriverError("get_html() called before navigate()")
        return self._last_response.text

    async def get_text(self, selector: str) -> str | None:
        """Parse last response HTML with selectolax and return first match text.

        This is a single-field transport convenience.  Full ``Article``
        extraction is done by the scraper layer (``utils/parsers.py``).

        Returns ``None`` when no element matches *selector*.
        """
        if self._last_response is None:
            raise DriverError("get_text() called before navigate()")

        from selectolax.parser import HTMLParser  # lazy import; selectolax is optional

        tree = HTMLParser(self._last_response.text)
        node = tree.css_first(selector)
        if node is None:
            return None
        return (node.text() or "").strip() or None

    # ------------------------------------------------------------------
    # Challenge solving
    # ------------------------------------------------------------------

    async def solve_challenge(self) -> bool:
        """Always returns ``False`` — HTTP drivers cannot execute JavaScript."""
        return False

    # ------------------------------------------------------------------
    # Cookie management
    # ------------------------------------------------------------------

    async def export_cookies(self) -> list[dict]:
        """Convert the session cookie jar to a list of dicts.

        Each dict has keys: ``name``, ``value``, ``domain``, ``path``,
        ``expires``, ``secure``.
        """
        if self._session is None:
            return []

        result: list[dict] = []
        for cookie in self._session.cookies:
            entry: dict = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
            }
            result.append(entry)
        return result

    async def import_cookies(self, cookies: list[dict]) -> None:
        """Load *cookies* into the session cookie jar."""
        if self._session is None:
            raise DriverError("import_cookies() called before launch()")

        for c in cookies:
            self._session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
            )

    # ------------------------------------------------------------------
    # Storage state
    # ------------------------------------------------------------------

    async def inject_storage_state(self, state: StorageState) -> None:
        """Inject cookies from *state*.

        curl_cffi has no localStorage/sessionStorage; only cookies are injected.
        """
        await self.import_cookies(state.cookies)

    async def export_storage_state(self) -> StorageState:
        """Return a ``StorageState`` containing the current session cookies.

        ``local_storage`` and ``session_storage`` are always empty (HTTP-only).
        """
        cookies = await self.export_cookies()
        # Derive domain from the first cookie, or leave blank.
        domain = cookies[0].get("domain", "") if cookies else ""
        return StorageState(domain=domain, cookies=cookies)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def screenshot(self, path: str | None = None) -> bytes:
        """Always raises — curl_cffi cannot capture screenshots (no rendering)."""
        raise DriverError("curl_cffi cannot capture screenshots (no rendering)")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the async session.  Idempotent — safe to call multiple times."""
        if self._session is not None:
            await self._session.close()
            self._session = None
