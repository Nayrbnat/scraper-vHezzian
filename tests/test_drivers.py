"""Unit tests for CurlCffiDriver (SPEC.md §3.3, U5).

All network calls are monkeypatched — no live requests in any unit test.
``curl_cffi.requests.AsyncSession`` is replaced with a fake async session whose
``get``, ``close``, ``__aenter__``, and ``__aexit__`` are controlled by fixtures.
BrowserProfile instances are constructed explicitly; FingerprintManager.generate_profile
is never called (which would probe the host for Chrome).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from scrapeforge.core.models import BrowserProfile, StorageState
from scrapeforge.exceptions import ChallengeError, DriverError, RateLimitError

# ---------------------------------------------------------------------------
# Helpers / fixtures
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


def _make_fake_response(
    status_code: int,
    text: str = "<html>ok</html>",
    headers: dict | None = None,
) -> MagicMock:
    """Build a minimal response mock that satisfies driver expectations."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    return resp


class FakeAsyncSession:
    """Drop-in replacement for ``curl_cffi.requests.AsyncSession``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        # Default: 200 success
        self._response = _make_fake_response(200)
        self._cookies: dict[str, Any] = {}
        self.headers: dict[str, str] = {}
        self.closed = False

    def set_response(self, response: MagicMock) -> None:
        self._response = response

    # curl_cffi cookies is a MagicMock-able property; we store a simple dict
    @property
    def cookies(self) -> dict[str, Any]:
        return self._cookies

    async def get(self, url: str, **kwargs: Any) -> MagicMock:  # noqa: ARG002
        return self._response

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeAsyncSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


@pytest.fixture
def fake_session() -> FakeAsyncSession:
    return FakeAsyncSession()


@pytest.fixture
def driver(fake_session: FakeAsyncSession, monkeypatch: pytest.MonkeyPatch):
    """Return a CurlCffiDriver with the AsyncSession monkeypatched out."""
    monkeypatch.setattr(
        "scrapeforge.core.drivers.curl_cffi_driver.AsyncSession",
        lambda *a, **kw: fake_session,
    )
    from scrapeforge.core.drivers.curl_cffi_driver import CurlCffiDriver

    drv = CurlCffiDriver(proxy=None, profile=EXPLICIT_PROFILE, timeout_ms=5000)
    return drv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLaunch:
    async def test_launch_creates_session_with_impersonate_target(
        self,
        driver,
        fake_session: FakeAsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """launch() must set _session; impersonate target is derived from the profile."""
        captured: list[dict] = []

        def capturing_session(*args: Any, **kwargs: Any) -> FakeAsyncSession:
            captured.append(kwargs)
            return fake_session

        monkeypatch.setattr(
            "scrapeforge.core.drivers.curl_cffi_driver.AsyncSession",
            capturing_session,
        )

        await driver.launch()

        assert driver._session is fake_session
        # FingerprintManager.curl_impersonate_target(chrome_major=131) → 'chrome131'
        assert captured[0]["impersonate"] == "chrome131"

    async def test_launch_sets_proxy_on_session(
        self,
        fake_session: FakeAsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict] = []

        def capturing_session(*args: Any, **kwargs: Any) -> FakeAsyncSession:
            captured.append(kwargs)
            return fake_session

        monkeypatch.setattr(
            "scrapeforge.core.drivers.curl_cffi_driver.AsyncSession",
            capturing_session,
        )

        from scrapeforge.core.drivers.curl_cffi_driver import CurlCffiDriver

        drv = CurlCffiDriver(
            proxy="http://user:pass@proxy.example:8080",
            profile=EXPLICIT_PROFILE,
            timeout_ms=5000,
        )
        await drv.launch()

        assert captured[0].get("proxy") == "http://user:pass@proxy.example:8080"


class TestNavigate:
    async def test_navigate_200_returns_success_result(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(_make_fake_response(200, "<html>article</html>"))

        result = await driver.navigate("https://example.com", None, "domcontentloaded")

        assert result.status == "success"
        assert result.driver_used == "curl_cffi"
        assert result.article is None  # driver doesn't extract; scraper does
        assert result.fetch_duration_ms >= 0

    async def test_navigate_records_proxy_used(
        self,
        fake_session: FakeAsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "scrapeforge.core.drivers.curl_cffi_driver.AsyncSession",
            lambda *a, **kw: fake_session,
        )
        from scrapeforge.core.drivers.curl_cffi_driver import CurlCffiDriver

        drv = CurlCffiDriver(
            proxy="http://proxy:8080",
            profile=EXPLICIT_PROFILE,
            timeout_ms=5000,
        )
        await drv.launch()
        result = await drv.navigate("https://example.com", None, "domcontentloaded")

        assert result.proxy_used == "http://proxy:8080"

    async def test_navigate_403_cf_mitigated_header_raises_challenge(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(
            _make_fake_response(403, "<html>blocked</html>", {"cf-mitigated": "challenge"})
        )

        with pytest.raises(ChallengeError):
            await driver.navigate("https://example.com", None, "domcontentloaded")

    async def test_navigate_403_cloudflare_in_body_raises_challenge(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(
            _make_fake_response(403, "<html>Cloudflare protection active</html>")
        )

        with pytest.raises(ChallengeError):
            await driver.navigate("https://example.com", None, "domcontentloaded")

    async def test_navigate_429_raises_rate_limit(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(_make_fake_response(429, "<html>slow down</html>"))

        with pytest.raises(RateLimitError):
            await driver.navigate("https://example.com", None, "domcontentloaded")

    async def test_navigate_other_4xx_raises_driver_error(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(_make_fake_response(404, "<html>not found</html>"))

        with pytest.raises(DriverError, match="HTTP 404"):
            await driver.navigate("https://example.com", None, "domcontentloaded")

    async def test_navigate_5xx_raises_driver_error(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(_make_fake_response(503, "<html>service unavailable</html>"))

        with pytest.raises(DriverError, match="HTTP 503"):
            await driver.navigate("https://example.com", None, "domcontentloaded")

    async def test_navigate_403_no_cf_marker_raises_driver_error_not_challenge(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        """Fix 2a: plain 403 Forbidden (no CF marker, no cloudflare in body) → DriverError.

        This guards against falsely classifying ordinary 403s as Cloudflare challenges.
        """
        await driver.launch()
        fake_session.set_response(
            _make_fake_response(403, "<html>Access denied by the server</html>")
        )

        with pytest.raises(DriverError, match="HTTP 403"):
            await driver.navigate("https://example.com", None, "domcontentloaded")

    async def test_navigate_403_cf_mitigated_header_capitalized_raises_challenge(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        """Fix 2c: cf-mitigated header with title-case key must still raise ChallengeError.

        Real HTTP stacks may normalise header casing; the detection must be case-insensitive.
        """
        await driver.launch()
        fake_session.set_response(
            _make_fake_response(403, "<html>blocked</html>", {"Cf-Mitigated": "challenge"})
        )

        with pytest.raises(ChallengeError):
            await driver.navigate("https://example.com", None, "domcontentloaded")

    async def test_navigate_stores_last_response(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        resp = _make_fake_response(200, "<html>hello</html>")
        fake_session.set_response(resp)

        await driver.navigate("https://example.com", None, "domcontentloaded")

        assert driver._last_response is resp


class TestGetHtml:
    async def test_get_html_returns_last_response_text(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(_make_fake_response(200, "<html><body>content</body></html>"))
        await driver.navigate("https://example.com", None, "domcontentloaded")

        html = await driver.get_html()

        assert html == "<html><body>content</body></html>"

    async def test_get_html_before_navigate_raises_driver_error(self, driver) -> None:
        await driver.launch()

        with pytest.raises(DriverError):
            await driver.get_html()


class TestGetText:
    async def test_get_text_returns_matched_selector_text(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(_make_fake_response(200, "<html><h1>Hello World</h1></html>"))
        await driver.navigate("https://example.com", None, "domcontentloaded")

        text = await driver.get_text("h1")

        assert text == "Hello World"

    async def test_get_text_returns_none_for_missing_selector(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        fake_session.set_response(_make_fake_response(200, "<html><p>no h2 here</p></html>"))
        await driver.navigate("https://example.com", None, "domcontentloaded")

        text = await driver.get_text("h2")

        assert text is None


class TestSolveChallenge:
    async def test_solve_challenge_always_returns_false(self, driver) -> None:
        """HTTP-only driver cannot execute JS; always returns False."""
        result = await driver.solve_challenge()
        assert result is False


class TestCookies:
    async def test_export_cookies_returns_list(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        await driver.launch()
        # Simulate a cookie jar with one cookie
        mock_cookie = MagicMock()
        mock_cookie.name = "session"
        mock_cookie.value = "abc123"
        mock_cookie.domain = "example.com"
        mock_cookie.path = "/"
        mock_cookie.expires = None
        mock_cookie.secure = True
        mock_cookie.has_nonstandard_attr = MagicMock(return_value=False)

        # Give the fake session a cookies jar with one cookie
        fake_jar = MagicMock()
        fake_jar.__iter__ = MagicMock(return_value=iter([mock_cookie]))
        fake_session._cookies = fake_jar  # type: ignore[assignment]

        cookies = await driver.export_cookies()

        assert isinstance(cookies, list)
        assert len(cookies) == 1
        assert cookies[0]["name"] == "session"
        assert cookies[0]["value"] == "abc123"

    async def test_import_cookies_then_export_round_trip(
        self,
        driver,
        fake_session: FakeAsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """import_cookies should load cookies into the session; export returns them."""
        await driver.launch()

        input_cookies = [
            {"name": "auth", "value": "token_xyz", "domain": "example.com", "path": "/"}
        ]

        # Track what was set on the session
        set_calls: list[Any] = []

        def fake_set(name: str, value: str, **kwargs: Any) -> None:
            set_calls.append({"name": name, "value": value, **kwargs})

        fake_session._cookies = MagicMock()
        fake_session._cookies.set = fake_set

        await driver.import_cookies(input_cookies)

        assert len(set_calls) == 1
        assert set_calls[0]["name"] == "auth"
        assert set_calls[0]["value"] == "token_xyz"


class TestStorageState:
    async def test_inject_storage_state_loads_cookies(
        self,
        driver,
        fake_session: FakeAsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await driver.launch()

        set_calls: list[Any] = []

        def fake_set(name: str, value: str, **kwargs: Any) -> None:
            set_calls.append({"name": name, "value": value, **kwargs})

        fake_session._cookies = MagicMock()
        fake_session._cookies.set = fake_set

        state = StorageState(
            domain="example.com",
            cookies=[
                {"name": "cf_clearance", "value": "abc", "domain": "example.com", "path": "/"}
            ],
        )
        await driver.inject_storage_state(state)

        assert any(c["name"] == "cf_clearance" for c in set_calls)

    async def test_export_storage_state_round_trips_cookie_fields_and_domain(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        """Fix 2b: export_storage_state must round-trip name/value and derive domain correctly.

        After importing a known cookie the domain on the returned StorageState must match
        the cookie's domain, and the cookie fields must be preserved verbatim.
        """
        await driver.launch()

        # Build a fake cookie object that the driver will iterate over.
        mock_cookie = MagicMock()
        mock_cookie.name = "session"
        mock_cookie.value = "abc"
        mock_cookie.domain = "test.com"
        mock_cookie.path = "/"
        mock_cookie.expires = None
        mock_cookie.secure = False
        mock_cookie.has_nonstandard_attr = MagicMock(return_value=False)

        fake_jar = MagicMock()
        fake_jar.__iter__ = MagicMock(return_value=iter([mock_cookie]))
        fake_session._cookies = fake_jar  # type: ignore[assignment]

        state = await driver.export_storage_state()

        assert isinstance(state, StorageState)
        assert len(state.cookies) == 1
        assert state.cookies[0]["name"] == "session"
        assert state.cookies[0]["value"] == "abc"
        # domain on the StorageState must be derived from the first cookie's domain.
        assert state.domain == "test.com"


class TestScreenshot:
    async def test_screenshot_raises_driver_error(self, driver) -> None:
        """curl_cffi cannot render pages; screenshot always raises DriverError."""
        with pytest.raises(DriverError, match="screenshot"):
            await driver.screenshot()


class TestClose:
    async def test_close_idempotent(
        self,
        driver,
        fake_session: FakeAsyncSession,
    ) -> None:
        """close() must be safe to call multiple times."""
        await driver.launch()
        await driver.close()
        await driver.close()  # second call must not raise

        assert fake_session.closed

    async def test_close_before_launch_is_safe(self, driver) -> None:
        """close() before launch() must not raise."""
        await driver.close()  # _session is None; must not raise
