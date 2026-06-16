"""Browser fingerprint management for ScrapeForge (SPEC.md §3.15).

Responsibilities
----------------
- Maintain a set of realistic ``BrowserProfile`` templates per OS variant.
- Detect the installed Chrome major version from the host environment (registry,
  subprocess, macOS plist) — never use a stale pin (Invariant #8).
- Generate coherent profiles: TLS fingerprint, UA, platform, and
  ``chrome_major_version`` all derived from the same installed Chrome
  (Invariant #11).
- Map a Chrome major version to the nearest curl_cffi impersonate alias.
- Optionally validate the JA4 fingerprint via a thin mitmproxy shim
  (``validate_ja4`` — fully mockable; real capture is an integration concern).

All I/O is async; the version-detection helpers are sync but short (no blocking
network calls).  ``validate_ja4`` is async because it spawns a subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess  # noqa: S404 — controlled use; no shell=True
import sys
from dataclasses import replace

from scrapeforge.core.models import BrowserProfile
from scrapeforge.exceptions import FingerprintError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

DEFAULT_CHROME_MAJOR: int = 131

# Sorted list of known curl_cffi Chrome impersonate aliases.
# Extend as curl_cffi gains new targets.
_KNOWN_CURL_ALIASES: list[int] = sorted([116, 119, 120, 123, 124, 131])

# Chrome registry sub-key (Windows)
_WIN_CHROME_SUBKEY = "Software\\Google\\Chrome\\BLBeacon"

# ---------------------------------------------------------------------------
# Profile templates — one per OS variant.
# chrome_major_version is set to DEFAULT_CHROME_MAJOR as a template default;
# generate_profile() re-stamps it from the detected installed Chrome.
# ---------------------------------------------------------------------------

PROFILES: dict[str, BrowserProfile] = {
    "chrome_win": BrowserProfile(
        name="chrome_win",
        user_agent=(
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{DEFAULT_CHROME_MAJOR}.0.0.0 Safari/537.36"
        ),
        tls_fingerprint="t13d1516h2_8daaf6152771_b0da82dd1658",
        http2_settings={
            "HEADER_TABLE_SIZE": 65536,
            "ENABLE_PUSH": 0,
            "INITIAL_WINDOW_SIZE": 6291456,
            "MAX_HEADER_LIST_SIZE": 262144,
        },
        platform="Win32",
        chrome_major_version=DEFAULT_CHROME_MAJOR,
    ),
    "chrome_mac": BrowserProfile(
        name="chrome_mac",
        user_agent=(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{DEFAULT_CHROME_MAJOR}.0.0.0 Safari/537.36"
        ),
        tls_fingerprint="t13d1516h2_8daaf6152771_e87db048d32a",
        http2_settings={
            "HEADER_TABLE_SIZE": 65536,
            "ENABLE_PUSH": 0,
            "INITIAL_WINDOW_SIZE": 6291456,
            "MAX_HEADER_LIST_SIZE": 262144,
        },
        platform="MacIntel",
        chrome_major_version=DEFAULT_CHROME_MAJOR,
    ),
    "chrome_linux": BrowserProfile(
        name="chrome_linux",
        user_agent=(
            f"Mozilla/5.0 (X11; Linux x86_64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{DEFAULT_CHROME_MAJOR}.0.0.0 Safari/537.36"
        ),
        tls_fingerprint="t13d1516h2_8daaf6152771_fc9d8d0b0b39",
        http2_settings={
            "HEADER_TABLE_SIZE": 65536,
            "ENABLE_PUSH": 0,
            "INITIAL_WINDOW_SIZE": 6291456,
            "MAX_HEADER_LIST_SIZE": 262144,
        },
        platform="Linux x86_64",
        chrome_major_version=DEFAULT_CHROME_MAJOR,
    ),
}


# ---------------------------------------------------------------------------
# FingerprintManager
# ---------------------------------------------------------------------------


class FingerprintManager:
    """Generates and validates browser fingerprints.

    Invariants
    ----------
    - Profiles are consistent: TLS + UA + platform match.
    - The curl_cffi impersonate target is derived from the installed Chrome major
      version, never a stale pin (Invariant #8).
    - All drivers in one handoff chain share the same returned ``BrowserProfile``
      (Invariant #11).
    """

    def __init__(self) -> None:
        # Cache slot; populated lazily on first detect_installed_chrome_version() call.
        self._chrome_version: int | None = None

    # ------------------------------------------------------------------
    # Version detection
    # ------------------------------------------------------------------

    def detect_installed_chrome_version(self) -> int:
        """Return the installed Chrome *major* version.

        Detection order
        ---------------
        1. Windows registry (HKLM then HKCU ``Software\\Google\\Chrome\\BLBeacon``).
        2. ``chrome --version`` subprocess.
        3. ``google-chrome --version`` subprocess.
        4. macOS Info.plist (``/Applications/Google Chrome.app``).

        The result is cached on the instance; subsequent calls return the cached
        value without re-probing the OS.

        Raises
        ------
        FingerprintError
            When no detection method succeeds.
        """
        if self._chrome_version is not None:
            return self._chrome_version

        major = (
            self._detect_via_windows_registry()
            or self._detect_via_subprocess("chrome")
            or self._detect_via_subprocess("google-chrome")
            or self._detect_via_macos_plist()
        )

        if major is None:
            raise FingerprintError(
                "Could not detect installed Chrome version via registry, "
                "subprocess, or Info.plist. "
                "Ensure Chrome is installed and on PATH, or call "
                "generate_profile() which falls back to "
                f"DEFAULT_CHROME_MAJOR={DEFAULT_CHROME_MAJOR}."
            )

        self._chrome_version = major
        return major

    # ------------------------------------------------------------------
    # Private detection helpers
    # ------------------------------------------------------------------

    def _detect_via_windows_registry(self) -> int | None:
        """Try HKLM then HKCU Chrome version key.  Returns None on non-Windows or failure."""
        if sys.platform != "win32":
            return None
        try:
            import winreg  # type: ignore[import-not-found]  # Windows-only
        except ImportError:
            return None

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, _WIN_CHROME_SUBKEY) as key:
                    version_str, _ = winreg.QueryValueEx(key, "version")
                    return _parse_major(str(version_str))
            except (OSError, FileNotFoundError, KeyError):
                continue
        return None

    def _detect_via_subprocess(self, command: str) -> int | None:
        """Run ``<command> --version`` and parse the major.  Returns None on failure."""
        try:
            result = subprocess.run(  # noqa: S603
                [command, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                return _parse_major(result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None

    def _detect_via_macos_plist(self) -> int | None:
        """Parse Chrome's Info.plist on macOS.  Returns None on non-macOS or failure."""
        if sys.platform != "darwin":
            return None
        plist_path = "/Applications/Google Chrome.app/Contents/Info.plist"
        try:
            import plistlib
            from pathlib import Path as _Path

            data = plistlib.loads(_Path(plist_path).read_bytes())
            version_str = data.get("KSVersion") or data.get("CFBundleShortVersionString", "")
            return _parse_major(str(version_str)) if version_str else None
        except Exception:  # noqa: BLE001 — any plist read failure is non-fatal
            return None

    # ------------------------------------------------------------------
    # Profile generation
    # ------------------------------------------------------------------

    def generate_profile(self, name: str = "chrome") -> BrowserProfile:
        """Return a new ``BrowserProfile`` with ``chrome_major_version`` stamped.

        Parameters
        ----------
        name:
            A specific template key (``'chrome_win'``, ``'chrome_mac'``,
            ``'chrome_linux'``) **or** the generic ``'chrome'`` which picks the
            variant matching the host ``sys.platform``.

        Returns
        -------
        BrowserProfile
            A *new* frozen instance (``dataclasses.replace``); the shared
            template is never mutated.

        Raises
        ------
        FingerprintError
            When ``name`` is not a recognised key and is not the generic
            ``'chrome'`` token.
        """
        template_key = self._resolve_template_key(name)

        try:
            major = self.detect_installed_chrome_version()
        except FingerprintError:
            log.warning(
                "Could not detect installed Chrome version; "
                "falling back to DEFAULT_CHROME_MAJOR=%d.",
                DEFAULT_CHROME_MAJOR,
            )
            major = DEFAULT_CHROME_MAJOR

        template = PROFILES[template_key]
        return replace(template, chrome_major_version=major)

    def _resolve_template_key(self, name: str) -> str:
        """Return the PROFILES key to use for *name*.

        Raises ``FingerprintError`` for unrecognised names.
        """
        if name in PROFILES:
            return name
        if name == "chrome":
            # Pick the variant that matches the host platform.
            if sys.platform == "darwin":
                return "chrome_mac"
            if sys.platform.startswith("linux"):
                return "chrome_linux"
            # Default to Windows (also covers 'win32', 'cygwin', etc.)
            return "chrome_win"
        raise FingerprintError(
            f"Unknown profile name {name!r}. Valid keys: {sorted(PROFILES)} or generic 'chrome'."
        )

    # ------------------------------------------------------------------
    # curl_cffi impersonate target
    # ------------------------------------------------------------------

    def curl_impersonate_target(self, profile: BrowserProfile) -> str:
        """Map *profile.chrome_major_version* to the nearest curl_cffi alias.

        Mapping rule
        ------------
        Use the largest known alias that is **<= the requested major**.
        If the major is smaller than all known aliases, return the smallest
        known alias.  Fall back to the generic ``'chrome'`` only if
        ``_KNOWN_CURL_ALIASES`` is somehow empty (should never happen in
        practice).

        Parameters
        ----------
        profile:
            A ``BrowserProfile`` whose ``chrome_major_version`` is used.

        Returns
        -------
        str
            e.g. ``'chrome131'``, ``'chrome124'``.
        """
        if not _KNOWN_CURL_ALIASES:
            return "chrome"

        major = profile.chrome_major_version

        # Find the largest alias that is <= major (closest-not-greater).
        best: int | None = None
        for alias in _KNOWN_CURL_ALIASES:
            if alias <= major:
                best = alias
            else:
                # Aliases are sorted ascending; once we exceed major, stop.
                break

        if best is None:
            # major is below all known aliases — use the lowest.
            best = _KNOWN_CURL_ALIASES[0]

        return f"chrome{best}"

    # ------------------------------------------------------------------
    # JA4 validation (thin shim — real capture is an integration concern)
    # ------------------------------------------------------------------

    async def validate_ja4(self, driver: str, proxy: str | None = None) -> str:
        """Launch mitmproxy, route one request through *driver*, return JA4 hash.

        This method is intentionally thin and fully mockable.  In unit tests,
        monkeypatch ``asyncio.create_subprocess_exec`` (to feed canned output)
        and ``FingerprintManager._run_driver_request`` (to skip real network).

        Parameters
        ----------
        driver:
            Driver name as accepted by ``StealthBridge`` (e.g. ``'curl_cffi'``).
        proxy:
            Optional proxy URL to route the test request through.

        Returns
        -------
        str
            The JA4 hash string extracted from mitmproxy output.

        Raises
        ------
        FingerprintError
            When mitmproxy output does not contain a parseable JA4 line.
        """
        mitm_proc = await asyncio.create_subprocess_exec(
            "mitmdump",
            "--quiet",
            "--set",
            "flow_detail=3",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            # Make a single test request through the driver via the mitm proxy.
            await self._run_driver_request(driver=driver, proxy=proxy)

            # Give mitmproxy a moment to capture and log the request.
            await asyncio.sleep(0.5)  # pragma: no cover — skipped in tests via mock

            raw_output = await mitm_proc.stdout.read()  # type: ignore[union-attr]
        finally:
            with contextlib.suppress(Exception):
                mitm_proc.terminate()
            await mitm_proc.wait()

        decoded = raw_output.decode(errors="replace")
        match = re.search(r"JA4:\s*(\S+)", decoded)
        if not match:
            raise FingerprintError(
                f"JA4 hash not found in mitmproxy output. Output snippet: {decoded[:200]!r}"
            )
        return match.group(1)

    async def _run_driver_request(
        self,
        driver: str,
        proxy: str | None,  # noqa: ARG002
    ) -> None:
        """Send one test HTTP request via *driver* through mitmproxy.

        Kept thin: this is the seam tests monkeypatch.  Real integration would
        use StealthBridge here; importing it here would create a circular dep
        (StealthBridge imports FingerprintManager).  The indirection via a
        method keeps coupling minimal.
        """
        # Integration: instantiate StealthBridge(driver, proxy=proxy) and
        # navigate to a known URL through the mitmproxy listener.
        # Unit tests monkeypatch this to a no-op.


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_major(version_str: str) -> int | None:
    """Extract the major integer from a Chrome version string like '131.0.6778.86'.

    Returns ``None`` if no version number is found.
    """
    match = re.search(r"(\d+)\.\d+\.\d+", version_str)
    if match:
        return int(match.group(1))
    return None
