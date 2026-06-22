"""Unit tests for StealthBridge (SPEC.md §3.1, U5).

BrowserProfile is constructed explicitly so FingerprintManager.generate_profile()
(which probes the host for Chrome) is never invoked in unit tests.

The backend (CurlCffiDriver) is monkeypatched so no real curl_cffi session is
created; all I/O is controlled by fake AsyncMocks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeforge.core.models import BrowserProfile, ScrapeResult, StorageState
from scrapeforge.exceptions import DriverError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPLICIT_PROFILE = BrowserProfile(
    name="chrome_win",
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
    tls_fingerprint="t13d1516h2_test",
    http2_settings={"HEADER_TABLE_SIZE": 65536},
    platform="Win32",
    chrome_major_version=131,
    accept_language="en-US,en;q=0.9",
)

FAKE_RESULT = ScrapeResult(status="success", driver_used="curl_cffi")


def _make_mock_driver() -> MagicMock:
    """Create an async-capable mock BaseDriver."""
    drv = MagicMock()
    drv.launch = AsyncMock()
    drv.close = AsyncMock()
    drv.navigate = AsyncMock(return_value=FAKE_RESULT)
    drv.get_html = AsyncMock(return_value="<html/>")
    drv.get_text = AsyncMock(return_value="some text")
    drv.solve_challenge = AsyncMock(return_value=False)
    drv.export_cookies = AsyncMock(return_value=[])
    drv.import_cookies = AsyncMock()
    drv.inject_storage_state = AsyncMock()
    drv.export_storage_state = AsyncMock(return_value=StorageState(domain="example.com"))
    drv.screenshot = AsyncMock(return_value=b"PNG")
    return drv


# ---------------------------------------------------------------------------
# Tests — construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_curl_cffi_driver_builds_curl_cffi_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """StealthBridge('curl_cffi') must create a CurlCffiDriver backend."""
        from scrapeforge.core.drivers.curl_cffi_driver import CurlCffiDriver
        from scrapeforge.core.stealth_bridge import StealthBridge

        bridge = StealthBridge("curl_cffi", profile=EXPLICIT_PROFILE)

        assert isinstance(bridge._backend, CurlCffiDriver)

    def test_patchright_raises_driver_error_at_construction(self) -> None:
        from scrapeforge.core.stealth_bridge import StealthBridge

        with pytest.raises(DriverError, match="patchright"):
            StealthBridge("patchright", profile=EXPLICIT_PROFILE)

    def test_nodriver_raises_driver_error_at_construction(self) -> None:
        from scrapeforge.core.stealth_bridge import StealthBridge

        with pytest.raises(DriverError, match="nodriver"):
            StealthBridge("nodriver", profile=EXPLICIT_PROFILE)

    def test_primp_raises_driver_error_at_construction(self) -> None:
        from scrapeforge.core.stealth_bridge import StealthBridge

        with pytest.raises(DriverError, match="primp"):
            StealthBridge("primp", profile=EXPLICIT_PROFILE)

    def test_unknown_driver_raises_driver_error(self) -> None:
        from scrapeforge.core.stealth_bridge import StealthBridge

        with pytest.raises(DriverError, match="unknown driver"):
            StealthBridge("totally_fake_driver", profile=EXPLICIT_PROFILE)

    def test_profile_stored_on_bridge(self) -> None:
        from scrapeforge.core.stealth_bridge import StealthBridge

        bridge = StealthBridge("curl_cffi", profile=EXPLICIT_PROFILE)

        assert bridge.profile is EXPLICIT_PROFILE


# ---------------------------------------------------------------------------
# Tests — async context manager
# ---------------------------------------------------------------------------


class TestAsyncContextManager:
    async def test_aenter_calls_backend_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_driver = _make_mock_driver()
        from scrapeforge.core import stealth_bridge as sb_module

        with patch.object(sb_module.StealthBridge, "_init_backend", return_value=mock_driver):
            from scrapeforge.core.stealth_bridge import StealthBridge

            bridge = StealthBridge("curl_cffi", profile=EXPLICIT_PROFILE)
            bridge._backend = mock_driver

            async with bridge:
                mock_driver.launch.assert_called_once()

    async def test_aexit_calls_close(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_driver = _make_mock_driver()
        from scrapeforge.core.stealth_bridge import StealthBridge

        bridge = StealthBridge("curl_cffi", profile=EXPLICIT_PROFILE)
        bridge._backend = mock_driver

        async with bridge:
            pass

        mock_driver.close.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — delegation
# ---------------------------------------------------------------------------


class TestDelegation:
    @pytest.fixture
    def bridge_with_mock(self) -> Any:
        from scrapeforge.core.stealth_bridge import StealthBridge

        bridge = StealthBridge("curl_cffi", profile=EXPLICIT_PROFILE)
        mock_driver = _make_mock_driver()
        bridge._backend = mock_driver
        return bridge, mock_driver

    async def test_navigate_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        result = await bridge.navigate("https://example.com")

        mock_driver.navigate.assert_called_once_with(
            "https://example.com", None, "domcontentloaded"
        )
        assert result is FAKE_RESULT

    async def test_get_html_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        html = await bridge.get_html()

        mock_driver.get_html.assert_called_once()
        assert html == "<html/>"

    async def test_get_text_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        text = await bridge.get_text("h1")

        mock_driver.get_text.assert_called_once_with("h1")
        assert text == "some text"

    async def test_solve_challenge_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        result = await bridge.solve_challenge()

        mock_driver.solve_challenge.assert_called_once()
        assert result is False

    async def test_export_cookies_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        cookies = await bridge.export_cookies()

        mock_driver.export_cookies.assert_called_once()
        assert cookies == []

    async def test_import_cookies_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        cookies = [{"name": "x", "value": "y"}]
        await bridge.import_cookies(cookies)

        mock_driver.import_cookies.assert_called_once_with(cookies)

    async def test_inject_storage_state_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        state = StorageState(domain="example.com")
        await bridge.inject_storage_state(state)

        mock_driver.inject_storage_state.assert_called_once_with(state)

    async def test_export_storage_state_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        state = await bridge.export_storage_state()

        mock_driver.export_storage_state.assert_called_once()
        assert isinstance(state, StorageState)

    async def test_screenshot_delegates_to_backend(self, bridge_with_mock: Any) -> None:
        bridge, mock_driver = bridge_with_mock
        data = await bridge.screenshot()

        mock_driver.screenshot.assert_called_once_with(None)
        assert data == b"PNG"


# ---------------------------------------------------------------------------
# Tests — close idempotency
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_idempotent(self) -> None:
        """close() must be safe to call multiple times."""
        from scrapeforge.core.stealth_bridge import StealthBridge

        bridge = StealthBridge("curl_cffi", profile=EXPLICIT_PROFILE)
        mock_driver = _make_mock_driver()
        bridge._backend = mock_driver

        await bridge.close()
        await bridge.close()  # second call must be a no-op

        mock_driver.close.assert_called_once()

    async def test_closed_flag_set_after_close(self) -> None:
        from scrapeforge.core.stealth_bridge import StealthBridge

        bridge = StealthBridge("curl_cffi", profile=EXPLICIT_PROFILE)
        bridge._backend = _make_mock_driver()

        assert not bridge._closed
        await bridge.close()
        assert bridge._closed
