"""Tests for scrapeforge.core.fingerprint_manager (U4).

TDD: these tests are written BEFORE the implementation.  They define the exact
contract; the implementation must satisfy them without modification.

Mocking strategy
----------------
- Windows-registry lookup  -> monkeypatch ``winreg`` (the real module or the
  stub the module imports).
- subprocess invocations   -> monkeypatch ``subprocess.run``.
- Platform discrimination  -> monkeypatch ``sys.platform``.
- asyncio.create_subprocess_exec -> monkeypatch to avoid spawning real processes.

No live network; no real mitmproxy; no real Chrome needed.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeforge.core.fingerprint_manager import DEFAULT_CHROME_MAJOR, PROFILES, FingerprintManager
from scrapeforge.core.models import BrowserProfile
from scrapeforge.exceptions import FingerprintError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> FingerprintManager:
    """Return a fresh FingerprintManager with no cached version."""
    m = FingerprintManager()
    m._chrome_version = None  # clear cache
    return m


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_chrome_major_is_int(self) -> None:
        assert isinstance(DEFAULT_CHROME_MAJOR, int)
        assert DEFAULT_CHROME_MAJOR >= 100  # sanity: not some tiny number

    def test_profiles_has_required_keys(self) -> None:
        assert set(PROFILES.keys()) == {"chrome_win", "chrome_mac", "chrome_linux"}

    def test_all_profiles_are_browser_profile_instances(self) -> None:
        for key, profile in PROFILES.items():
            assert isinstance(profile, BrowserProfile), f"{key} is not a BrowserProfile"

    def test_profiles_have_coherent_platforms(self) -> None:
        assert PROFILES["chrome_win"].platform == "Win32"
        assert PROFILES["chrome_mac"].platform == "MacIntel"
        assert PROFILES["chrome_linux"].platform == "Linux x86_64"

    def test_profiles_have_non_empty_user_agent(self) -> None:
        for key, profile in PROFILES.items():
            assert profile.user_agent, f"{key} has empty user_agent"
            assert "Chrome" in profile.user_agent, f"{key} UA doesn't contain 'Chrome'"

    def test_profiles_have_non_empty_tls_fingerprint(self) -> None:
        for key, profile in PROFILES.items():
            assert profile.tls_fingerprint, f"{key} has empty tls_fingerprint"

    def test_profiles_have_http2_settings_dict(self) -> None:
        for key, profile in PROFILES.items():
            assert isinstance(profile.http2_settings, dict), f"{key} http2_settings is not a dict"
            assert len(profile.http2_settings) > 0, f"{key} http2_settings is empty"

    def test_profiles_have_default_chrome_major(self) -> None:
        for key, profile in PROFILES.items():
            assert profile.chrome_major_version == DEFAULT_CHROME_MAJOR, (
                f"{key} template version != DEFAULT_CHROME_MAJOR"
            )


# ---------------------------------------------------------------------------
# detect_installed_chrome_version
# ---------------------------------------------------------------------------


class TestDetectInstalledChromeVersion:
    # --- Windows registry path ---

    def test_windows_hklm_registry_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Parses version from HKLM registry key (Windows)."""
        monkeypatch.setattr(sys, "platform", "win32")

        # Build a fake winreg module
        fake_winreg = types.ModuleType("winreg")
        fake_winreg.HKEY_LOCAL_MACHINE = 0
        fake_winreg.HKEY_CURRENT_USER = 1
        fake_key = MagicMock()
        fake_winreg.OpenKey = MagicMock(return_value=fake_key)
        fake_winreg.QueryValueEx = MagicMock(return_value=("131.0.6778.86", 1))
        fake_key.__enter__ = MagicMock(return_value=fake_key)
        fake_key.__exit__ = MagicMock(return_value=False)

        monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

        m = _make_manager()
        version = m.detect_installed_chrome_version()
        assert version == 131

    def test_windows_hkcu_fallback_when_hklm_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to HKCU when HKLM raises FileNotFoundError."""
        monkeypatch.setattr(sys, "platform", "win32")

        fake_winreg = types.ModuleType("winreg")
        fake_winreg.HKEY_LOCAL_MACHINE = 0
        fake_winreg.HKEY_CURRENT_USER = 1
        fake_key = MagicMock()
        fake_key.__enter__ = MagicMock(return_value=fake_key)
        fake_key.__exit__ = MagicMock(return_value=False)

        call_count = {"n": 0}

        def open_key(hive, subkey):
            call_count["n"] += 1
            if hive == fake_winreg.HKEY_LOCAL_MACHINE:
                raise FileNotFoundError("no key")
            return fake_key

        fake_winreg.OpenKey = open_key
        fake_winreg.QueryValueEx = MagicMock(return_value=("119.0.6045.160", 1))
        monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

        m = _make_manager()
        version = m.detect_installed_chrome_version()
        assert version == 119

    # --- subprocess path ---

    def test_subprocess_chrome_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Parses major from 'Google Chrome 131.0.6778.86' subprocess output."""
        monkeypatch.setattr(sys, "platform", "linux")

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "Google Chrome 131.0.6778.86 "

        with patch("subprocess.run", return_value=fake_result) as mock_run:
            m = _make_manager()
            version = m.detect_installed_chrome_version()

        assert version == 131
        assert mock_run.called

    def test_subprocess_google_chrome_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to google-chrome when chrome --version fails."""
        monkeypatch.setattr(sys, "platform", "linux")

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stdout = ""

        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stdout = "Google Chrome 124.0.0.0"

        side_effects = [fail_result, ok_result]

        with patch("subprocess.run", side_effect=side_effects):
            m = _make_manager()
            version = m.detect_installed_chrome_version()

        assert version == 124

    # --- all lookups fail ---

    def test_raises_fingerprint_error_when_all_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Raises FingerprintError when every detection method fails."""
        monkeypatch.setattr(sys, "platform", "linux")

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stdout = ""

        with patch("subprocess.run", return_value=fail_result):
            m = _make_manager()
            with pytest.raises(FingerprintError):
                m.detect_installed_chrome_version()

    # --- caching ---

    def test_version_is_cached_after_first_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """detect_installed_chrome_version() calls detection once; re-uses cached value."""
        monkeypatch.setattr(sys, "platform", "linux")

        ok_result = MagicMock()
        ok_result.returncode = 0
        ok_result.stdout = "Google Chrome 120.0.0.0"

        with patch("subprocess.run", return_value=ok_result) as mock_run:
            m = _make_manager()
            v1 = m.detect_installed_chrome_version()
            v2 = m.detect_installed_chrome_version()

        assert v1 == v2 == 120
        # subprocess.run should be called only during the first detection
        assert mock_run.call_count <= 2  # at most 2 candidates; second call uses cache


# ---------------------------------------------------------------------------
# generate_profile
# ---------------------------------------------------------------------------


class TestGenerateProfile:
    def test_returns_browser_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ok = MagicMock(returncode=0, stdout="Google Chrome 131.0.0.0")
        with patch("subprocess.run", return_value=ok):
            m = _make_manager()
            profile = m.generate_profile("chrome")
        assert isinstance(profile, BrowserProfile)

    def test_stamps_detected_major(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """chrome_major_version must equal what detect_installed_chrome_version() returned."""
        monkeypatch.setattr(sys, "platform", "linux")
        ok = MagicMock(returncode=0, stdout="Google Chrome 123.0.0.0")
        with patch("subprocess.run", return_value=ok):
            m = _make_manager()
            profile = m.generate_profile("chrome")
        assert profile.chrome_major_version == 123

    def test_falls_back_to_default_on_fingerprint_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When detection fails, falls back to DEFAULT_CHROME_MAJOR and logs a warning."""
        monkeypatch.setattr(sys, "platform", "linux")
        fail = MagicMock(returncode=1, stdout="")
        import logging

        with patch("subprocess.run", return_value=fail), caplog.at_level(logging.WARNING):
            m = _make_manager()
            profile = m.generate_profile("chrome")
        assert profile.chrome_major_version == DEFAULT_CHROME_MAJOR

    def test_explicit_key_selects_correct_variant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passing 'chrome_mac' returns a profile with MacIntel platform."""
        monkeypatch.setattr(sys, "platform", "win32")

        fake_winreg = types.ModuleType("winreg")
        fake_winreg.HKEY_LOCAL_MACHINE = 0
        fake_winreg.HKEY_CURRENT_USER = 1
        fake_key = MagicMock()
        fake_key.__enter__ = MagicMock(return_value=fake_key)
        fake_key.__exit__ = MagicMock(return_value=False)
        fake_winreg.OpenKey = MagicMock(return_value=fake_key)
        fake_winreg.QueryValueEx = MagicMock(return_value=("131.0.6778.86", 1))
        monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

        m = _make_manager()
        profile = m.generate_profile("chrome_mac")
        assert profile.platform == "MacIntel"

    def test_generic_chrome_on_win32_returns_chrome_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")

        fake_winreg = types.ModuleType("winreg")
        fake_winreg.HKEY_LOCAL_MACHINE = 0
        fake_winreg.HKEY_CURRENT_USER = 1
        fake_key = MagicMock()
        fake_key.__enter__ = MagicMock(return_value=fake_key)
        fake_key.__exit__ = MagicMock(return_value=False)
        fake_winreg.OpenKey = MagicMock(return_value=fake_key)
        fake_winreg.QueryValueEx = MagicMock(return_value=("131.0.6778.86", 1))
        monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

        m = _make_manager()
        profile = m.generate_profile("chrome")
        assert profile.platform == "Win32"

    def test_generic_chrome_on_darwin_returns_chrome_mac(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")

        fail = MagicMock(returncode=1, stdout="")
        ok = MagicMock(returncode=0, stdout="Google Chrome 131.0.0.0")
        with patch("subprocess.run", side_effect=[fail, ok]):
            m = _make_manager()
            profile = m.generate_profile("chrome")
        assert profile.platform == "MacIntel"

    def test_generic_chrome_on_linux_returns_chrome_linux(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ok = MagicMock(returncode=0, stdout="Google Chrome 131.0.0.0")
        with patch("subprocess.run", return_value=ok):
            m = _make_manager()
            profile = m.generate_profile("chrome")
        assert profile.platform == "Linux x86_64"

    def test_returned_profile_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ok = MagicMock(returncode=0, stdout="Google Chrome 131.0.0.0")
        with patch("subprocess.run", return_value=ok):
            m = _make_manager()
            profile = m.generate_profile("chrome")
        # frozen=True raises FrozenInstanceError (AttributeError subclass) on direct assignment.
        with pytest.raises((AttributeError, TypeError)):
            profile.platform = "changed"  # type: ignore[misc]

    def test_each_call_returns_new_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """generate_profile() must return a NEW object each call (dataclasses.replace)."""
        monkeypatch.setattr(sys, "platform", "linux")
        ok = MagicMock(returncode=0, stdout="Google Chrome 131.0.0.0")
        with patch("subprocess.run", return_value=ok):
            m = _make_manager()
            p1 = m.generate_profile("chrome")
            p2 = m.generate_profile("chrome")
        assert p1 is not p2

    def test_unknown_key_raises_fingerprint_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        ok = MagicMock(returncode=0, stdout="Google Chrome 131.0.0.0")
        with patch("subprocess.run", return_value=ok):
            m = _make_manager()
            with pytest.raises(FingerprintError):
                m.generate_profile("firefox_win")


# ---------------------------------------------------------------------------
# curl_impersonate_target
# ---------------------------------------------------------------------------


class TestCurlImpersonateTarget:
    def _profile_with_version(self, version: int) -> BrowserProfile:
        """Create a minimal BrowserProfile with a given chrome_major_version."""
        return replace(PROFILES["chrome_win"], chrome_major_version=version)

    def test_exact_match_131(self) -> None:
        m = FingerprintManager()
        profile = self._profile_with_version(131)
        assert m.curl_impersonate_target(profile) == "chrome131"

    def test_exact_match_124(self) -> None:
        m = FingerprintManager()
        profile = self._profile_with_version(124)
        assert m.curl_impersonate_target(profile) == "chrome124"

    def test_exact_match_116(self) -> None:
        m = FingerprintManager()
        profile = self._profile_with_version(116)
        assert m.curl_impersonate_target(profile) == "chrome116"

    def test_closest_not_greater_for_between_value(self) -> None:
        """Major 121 is between 120 and 123; closest-not-greater is 120."""
        m = FingerprintManager()
        profile = self._profile_with_version(121)
        result = m.curl_impersonate_target(profile)
        assert result == "chrome120"

    def test_closest_not_greater_for_value_below_all(self) -> None:
        """Major 100 is below all known aliases; should return lowest known."""
        m = FingerprintManager()
        profile = self._profile_with_version(100)
        result = m.curl_impersonate_target(profile)
        # Should return the lowest alias in the list
        assert result.startswith("chrome")
        major = int(result.replace("chrome", ""))
        assert major <= 116  # 116 is the expected lowest in the contract

    def test_above_all_known_returns_highest(self) -> None:
        """Major 999 is above all known; return highest known alias."""
        m = FingerprintManager()
        profile = self._profile_with_version(999)
        result = m.curl_impersonate_target(profile)
        assert result.startswith("chrome")
        major = int(result.replace("chrome", ""))
        assert major >= 131

    def test_result_is_string(self) -> None:
        m = FingerprintManager()
        profile = self._profile_with_version(DEFAULT_CHROME_MAJOR)
        result = m.curl_impersonate_target(profile)
        assert isinstance(result, str)
        assert result.startswith("chrome")


# ---------------------------------------------------------------------------
# validate_ja4
# ---------------------------------------------------------------------------


class TestValidateJa4:
    @pytest.mark.asyncio
    async def test_returns_parsed_ja4_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """validate_ja4 parses JA4 from subprocess output and returns it."""
        expected_hash = "t13d1516h2_8daaf6152771_b0da82dd1658"

        # Fake async process that yields the JA4 line in stdout
        fake_stdout = MagicMock()
        fake_stdout.read = AsyncMock(return_value=f"JA4: {expected_hash}\n".encode())

        fake_proc = MagicMock()
        fake_proc.stdout = fake_stdout
        fake_proc.returncode = 0
        fake_proc.wait = AsyncMock(return_value=0)
        fake_proc.terminate = MagicMock()

        async def fake_create_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess)

        # Also patch the driver call inside validate_ja4 — we use a simple sentinel
        with patch(
            "scrapeforge.core.fingerprint_manager.FingerprintManager._run_driver_request",
            new_callable=AsyncMock,
            return_value=None,
        ):
            m = FingerprintManager()
            result = await m.validate_ja4(driver="curl_cffi", proxy=None)

        assert result == expected_hash

    @pytest.mark.asyncio
    async def test_raises_fingerprint_error_when_ja4_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate_ja4 raises FingerprintError if JA4 line not in output."""
        fake_stdout = MagicMock()
        fake_stdout.read = AsyncMock(return_value=b"no ja4 here\n")

        fake_proc = MagicMock()
        fake_proc.stdout = fake_stdout
        fake_proc.returncode = 0
        fake_proc.wait = AsyncMock(return_value=0)
        fake_proc.terminate = MagicMock()

        async def fake_create_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess)

        with patch(
            "scrapeforge.core.fingerprint_manager.FingerprintManager._run_driver_request",
            new_callable=AsyncMock,
            return_value=None,
        ):
            m = FingerprintManager()
            with pytest.raises(FingerprintError):
                await m.validate_ja4(driver="curl_cffi", proxy=None)
