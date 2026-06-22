"""Tests for scrapeforge.core.proxy_rotator (U4).

TDD: tests written BEFORE the implementation.

Mocking strategy
----------------
- curl_cffi.requests.AsyncSession -> monkeypatched to avoid any real network.
- Settings -> pass an explicit tmp_path to ProxyRotator.__init__ so STATE_STORE_KEY
  is never needed.
- Time-sensitive cooldown -> freeze or monkeypatch datetime.now().
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeforge.core.models import ProxySession
from scrapeforge.core.proxy_rotator import ProxyRotator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_PROXY_LINE = "http://user:pass@192.168.1.1:8080"
VALID_PROXY_LINE_2 = "socks5://user2:pass2@10.0.0.1:1080"
VALID_PROXY_LINE_US = "http://user3:pass3@1.2.3.4:8080"


def _write_proxy_file(path: Path, lines: list[str]) -> Path:
    proxy_file = path / "proxies.txt"
    proxy_file.write_text("\n".join(lines), encoding="utf-8")
    return proxy_file


def _make_rotator(path: Path) -> ProxyRotator:
    return ProxyRotator(proxy_list_path=path)


def _fake_session_cls(status_code: int = 200, exc: Exception | None = None) -> MagicMock:
    """Build a fake AsyncSession class whose context manager yields a session.

    The yielded session's .get() either returns a response with the given
    status_code or raises *exc* if provided.
    """
    fake_response = MagicMock()
    fake_response.status_code = status_code

    fake_session_instance = MagicMock()
    if exc is not None:
        fake_session_instance.get = AsyncMock(side_effect=exc)
    else:
        fake_session_instance.get = AsyncMock(return_value=fake_response)
    fake_session_instance.__aenter__ = AsyncMock(return_value=fake_session_instance)
    fake_session_instance.__aexit__ = AsyncMock(return_value=False)

    cls = MagicMock(return_value=fake_session_instance)
    cls._instance = fake_session_instance  # expose for assertions
    return cls


# ---------------------------------------------------------------------------
# _load_proxies / parsing
# ---------------------------------------------------------------------------


class TestLoadProxies:
    def test_parses_valid_lines(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE, VALID_PROXY_LINE_2])
        r = _make_rotator(proxy_file)
        assert len(r.proxies) == 2
        assert r.proxies[0].url == VALID_PROXY_LINE
        assert r.proxies[1].url == VALID_PROXY_LINE_2

    def test_ignores_blank_lines(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, ["", VALID_PROXY_LINE, "", ""])
        r = _make_rotator(proxy_file)
        assert len(r.proxies) == 1

    def test_ignores_comment_lines(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(
            tmp_path,
            [
                "# this is a comment",
                VALID_PROXY_LINE,
                "# another comment",
                VALID_PROXY_LINE_2,
            ],
        )
        r = _make_rotator(proxy_file)
        assert len(r.proxies) == 2

    def test_ignores_inline_comments_after_whitespace(self, tmp_path: Path) -> None:
        """Lines starting with # after stripping are treated as comments."""
        proxy_file = _write_proxy_file(
            tmp_path,
            ["  # indented comment", VALID_PROXY_LINE],
        )
        r = _make_rotator(proxy_file)
        assert len(r.proxies) == 1

    def test_missing_file_yields_empty_list(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.txt"
        r = _make_rotator(missing)
        assert r.proxies == []

    def test_initial_health_status_is_unknown(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        assert r.proxies[0].health_status == "unknown"

    def test_all_proxies_are_proxy_session_instances(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE, VALID_PROXY_LINE_2])
        r = _make_rotator(proxy_file)
        for p in r.proxies:
            assert isinstance(p, ProxySession)


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_returns_true_on_200(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        fake_cls = _fake_session_cls(status_code=200)

        with patch("scrapeforge.core.proxy_rotator.AsyncSession", fake_cls):
            result = await r.health_check(proxy)

        assert result is True
        assert proxy.health_status == "healthy"
        # Verify the context manager was entered and exited (session closed)
        fake_cls._instance.__aenter__.assert_called_once()
        fake_cls._instance.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        fake_cls = _fake_session_cls(status_code=403)

        with patch("scrapeforge.core.proxy_rotator.AsyncSession", fake_cls):
            result = await r.health_check(proxy)

        assert result is False
        assert proxy.health_status == "unhealthy"
        fake_cls._instance.__aenter__.assert_called_once()
        fake_cls._instance.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        fake_cls = _fake_session_cls(exc=OSError("connection refused"))

        with patch("scrapeforge.core.proxy_rotator.AsyncSession", fake_cls):
            result = await r.health_check(proxy)

        assert result is False
        assert proxy.health_status == "unhealthy"
        # __aexit__ must be called even when session.get() raises
        fake_cls._instance.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_marks_failure_count_on_unhealthy(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        fake_cls = _fake_session_cls(exc=OSError("timeout"))

        with patch("scrapeforge.core.proxy_rotator.AsyncSession", fake_cls):
            await r.health_check(proxy)

        assert proxy.failure_count >= 1


# ---------------------------------------------------------------------------
# Settings resolved once in __init__
# ---------------------------------------------------------------------------


class TestSettingsResolvedOnce:
    def test_health_url_stored_on_init(self, tmp_path: Path) -> None:
        """_health_url must be set during __init__, not re-resolved per call."""
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        assert hasattr(r, "_health_url")
        assert isinstance(r._health_url, str)
        assert r._health_url  # non-empty

    def test_cooldown_minutes_stored_on_init(self, tmp_path: Path) -> None:
        """_cooldown_minutes must be set during __init__, not re-resolved per call."""
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        assert hasattr(r, "_cooldown_minutes")
        assert isinstance(r._cooldown_minutes, int)
        assert r._cooldown_minutes > 0

    def test_fallback_defaults_used_when_settings_unavailable(self, tmp_path: Path) -> None:
        """When Settings() raises ValidationError, documented defaults are used."""
        from pydantic import ValidationError

        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])

        with patch(
            "scrapeforge.core.proxy_rotator.Settings",
            side_effect=ValidationError.from_exception_data("Settings", [], input_type="python"),
        ):
            r = ProxyRotator(proxy_list_path=proxy_file)

        assert r._health_url == "https://httpbin.org/ip"
        assert r._cooldown_minutes == 60

    @pytest.mark.asyncio
    async def test_health_check_uses_stored_url(self, tmp_path: Path) -> None:
        """health_check must use self._health_url, not re-call Settings()."""
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        r._health_url = "https://example-custom.org/check"

        fake_cls = _fake_session_cls(status_code=200)

        with patch("scrapeforge.core.proxy_rotator.AsyncSession", fake_cls):
            await r.health_check(r.proxies[0])

        fake_cls._instance.get.assert_called_once_with("https://example-custom.org/check")


# ---------------------------------------------------------------------------
# get_healthy_proxy
# ---------------------------------------------------------------------------


class TestGetHealthyProxy:
    def _mock_health_check_true(self, r: ProxyRotator) -> None:
        """Patch health_check on the rotator to always return True."""

        async def _check_true(p: ProxySession) -> bool:
            p.health_status = "healthy"
            return True

        r.health_check = _check_true  # type: ignore[method-assign]

    def _mock_health_check_false(self, r: ProxyRotator) -> None:
        async def _check_false(p: ProxySession) -> bool:
            p.health_status = "unhealthy"
            return False

        r.health_check = _check_false  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_returns_first_healthy_proxy(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE, VALID_PROXY_LINE_2])
        r = _make_rotator(proxy_file)
        self._mock_health_check_true(r)

        result = await r.get_healthy_proxy()

        assert result is not None
        assert result.url == VALID_PROXY_LINE
        assert result.health_status == "healthy"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_healthy(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE, VALID_PROXY_LINE_2])
        r = _make_rotator(proxy_file)
        self._mock_health_check_false(r)

        result = await r.get_healthy_proxy()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "empty.txt"
        r = _make_rotator(missing)
        result = await r.get_healthy_proxy()
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_burned_when_exclude_burned(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE, VALID_PROXY_LINE_2])
        r = _make_rotator(proxy_file)
        self._mock_health_check_true(r)

        # Mark first proxy as burned via the rotator's own API so _burn_times is set.
        r.mark_burned(r.proxies[0])

        result = await r.get_healthy_proxy(exclude_burned=True)

        assert result is not None
        assert result.url == VALID_PROXY_LINE_2

    @pytest.mark.asyncio
    async def test_includes_burned_when_exclude_burned_false(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        self._mock_health_check_true(r)

        r.proxies[0].health_status = "burned"

        result = await r.get_healthy_proxy(exclude_burned=False)

        assert result is not None

    @pytest.mark.asyncio
    async def test_filters_by_country_code(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE, VALID_PROXY_LINE_2])
        r = _make_rotator(proxy_file)
        self._mock_health_check_true(r)

        # Assign country codes manually
        r.proxies[0].country_code = "DE"
        r.proxies[1].country_code = "US"

        result = await r.get_healthy_proxy(country_code="US")

        assert result is not None
        assert result.country_code == "US"
        assert result.url == VALID_PROXY_LINE_2

    @pytest.mark.asyncio
    async def test_returns_none_when_country_filter_matches_nothing(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        self._mock_health_check_true(r)

        r.proxies[0].country_code = "DE"

        result = await r.get_healthy_proxy(country_code="US")

        assert result is None

    @pytest.mark.asyncio
    async def test_sets_assigned_scraper_on_result(self, tmp_path: Path) -> None:
        """get_healthy_proxy does NOT set assigned_scraper — that's the engine's job.
        But last_used should be stamped.
        """
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        self._mock_health_check_true(r)

        result = await r.get_healthy_proxy()

        assert result is not None
        assert result.last_used is not None


# ---------------------------------------------------------------------------
# mark_burned
# ---------------------------------------------------------------------------


class TestMarkBurned:
    def test_sets_health_status_burned(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        r.mark_burned(proxy)

        assert proxy.health_status == "burned"

    def test_stamps_burned_at(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        before = datetime.now(UTC)
        r.mark_burned(proxy)
        after = datetime.now(UTC)

        burned_at = r._burn_times.get(id(proxy))
        assert burned_at is not None
        assert before <= burned_at <= after

    @pytest.mark.asyncio
    async def test_burned_proxy_excluded_within_cooldown(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        # Set up health_check to return True (so it's not excluded for health reasons)
        async def _check_true(p: ProxySession) -> bool:
            p.health_status = "healthy"
            return True

        r.health_check = _check_true  # type: ignore[method-assign]

        r.mark_burned(proxy)

        result = await r.get_healthy_proxy(exclude_burned=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_burned_proxy_re_eligible_after_cooldown(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        async def _check_true(p: ProxySession) -> bool:
            p.health_status = "healthy"
            return True

        r.health_check = _check_true  # type: ignore[method-assign]

        r.mark_burned(proxy)
        # Backdate the burn time in the rotator's own dict to simulate cooldown expiry.
        r._burn_times[id(proxy)] = datetime.now(UTC) - timedelta(hours=2)

        result = await r.get_healthy_proxy(exclude_burned=True)
        assert result is not None

    def test_burn_times_entry_removed_after_cooldown_expires(self, tmp_path: Path) -> None:
        """When cooldown expires and proxy is restored to 'unknown', its stale
        _burn_times entry must be popped to prevent unbounded dict growth.
        """
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        r.mark_burned(proxy)
        # Simulate expired cooldown by backdating the entry.
        r._burn_times[id(proxy)] = datetime.now(UTC) - timedelta(hours=2)

        # Trigger _filter_candidates (which handles cooldown expiry).
        r._filter_candidates(country_code=None, exclude_burned=True)

        assert id(proxy) not in r._burn_times


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


class TestRelease:
    def test_clears_assigned_scraper(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]
        proxy.assigned_scraper = "SomeScraper"

        r.release(proxy)

        assert proxy.assigned_scraper is None

    def test_updates_last_used(self, tmp_path: Path) -> None:
        proxy_file = _write_proxy_file(tmp_path, [VALID_PROXY_LINE])
        r = _make_rotator(proxy_file)
        proxy = r.proxies[0]

        before = datetime.now(UTC)
        r.release(proxy)
        after = datetime.now(UTC)

        assert proxy.last_used is not None
        assert before <= proxy.last_used <= after
