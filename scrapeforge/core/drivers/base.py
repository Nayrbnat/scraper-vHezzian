"""Abstract base class for all ScrapeForge driver backends (SPEC.md §3.2).

``BaseDriver`` is the internal contract that ``StealthBridge`` delegates to.
Scrapers never import this directly — they talk only to the bridge.

All I/O methods are ``async def``; no blocking calls on the event loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from scrapeforge.core.models import BrowserProfile, ScrapeResult, StorageState


class BaseDriver(ABC):
    """Abstract base for all driver backends.

    Subclasses implement every I/O method for their specific backend
    (curl_cffi, primp, patchright, nodriver).  ``StealthBridge`` is the only
    consumer; scrapers must not reference ``BaseDriver`` directly.
    """

    def __init__(
        self,
        proxy: str | None,
        profile: BrowserProfile,
        timeout_ms: int,
    ) -> None:
        self.proxy = proxy
        self.profile = profile
        self.timeout_ms = timeout_ms

    @abstractmethod
    async def launch(self) -> None:
        """Establish the session or browser (async I/O).

        Called once by ``StealthBridge.__aenter__`` before any navigation.
        """

    @abstractmethod
    async def navigate(
        self,
        url: str,
        wait_for: str | None,
        wait_until: str,
    ) -> ScrapeResult:
        """Navigate to *url* and return a ``ScrapeResult``.

        Raises
        ------
        ChallengeError
            Anti-bot gate detected.
        RateLimitError
            HTTP 429 received.
        DriverError
            Any other HTTP >= 400 or I/O error.
        """

    @abstractmethod
    async def get_html(self) -> str:
        """Return the raw HTML of the last navigated page.

        Raises
        ------
        DriverError
            When called before a successful ``navigate()``.
        """

    @abstractmethod
    async def get_text(self, selector: str) -> str | None:
        """Extract text from the first element matching *selector*.

        Returns ``None`` when no element matches.
        """

    @abstractmethod
    async def solve_challenge(self) -> bool:
        """Attempt to solve an anti-bot challenge.

        HTTP-only drivers (curl_cffi, primp) always return ``False``.
        Browser drivers (patchright, nodriver) attempt human-like interaction.
        """

    @abstractmethod
    async def export_cookies(self) -> list[dict]:
        """Export cookies in Netscape-compatible format.

        Returns a list of dicts with keys: ``name``, ``value``, ``domain``,
        ``path``, ``expires``, ``httpOnly``, ``secure``.
        """

    @abstractmethod
    async def import_cookies(self, cookies: list[dict]) -> None:
        """Load *cookies* into the current session."""

    @abstractmethod
    async def inject_storage_state(self, state: StorageState) -> None:
        """Inject cookies (and where supported, localStorage) from *state*."""

    @abstractmethod
    async def export_storage_state(self) -> StorageState:
        """Export current session state as a ``StorageState``."""

    @abstractmethod
    async def screenshot(self, path: str | None = None) -> bytes:
        """Capture a PNG screenshot.

        If *path* is given, also saves the bytes to disk.

        Raises
        ------
        DriverError
            For HTTP-only drivers that cannot render pages.
        """

    @abstractmethod
    async def close(self) -> None:
        """Deterministic cleanup.  Idempotent — safe to call multiple times."""
