"""Tests for scrapeforge.core.models — the canonical data structures.

TDD: written before the implementation exists; running these should fail with ImportError
until scrapeforge/core/models.py is created.

Coverage targets (per CLAUDE.md §3):
- Construct each dataclass with minimal required args; assert defaults.
- Frozen dataclasses raise FrozenInstanceError / AttributeError on mutation.
- ProxySession is mutable and mutations persist.
- StorageState.created_at is timezone-aware.
- ScrapeResult field-ordering regression (non-default fields first).
- Article.metadata uses default_factory (two instances share no state).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from scrapeforge.core.models import (
    Article,
    BrowserProfile,
    ProxySession,
    ScrapeResult,
    StorageState,
)

# ---------------------------------------------------------------------------
# Article
# ---------------------------------------------------------------------------


class TestArticle:
    def test_minimal_construction(self) -> None:
        """Article constructs with only required fields."""
        a = Article(url="https://example.com", title="Hello", content="World")
        assert a.url == "https://example.com"
        assert a.title == "Hello"
        assert a.content == "World"

    def test_optional_defaults_are_none(self) -> None:
        """Optional fields default to None."""
        a = Article(url="https://example.com", title="T", content="C")
        assert a.author is None
        assert a.publish_date is None
        assert a.raw_html is None

    def test_metadata_defaults_to_empty_dict(self) -> None:
        """metadata defaults to an empty dict."""
        a = Article(url="https://example.com", title="T", content="C")
        assert a.metadata == {}

    def test_metadata_default_factory_not_shared(self) -> None:
        """Two Article instances must NOT share the same metadata dict object."""
        a1 = Article(url="https://a.com", title="A", content="A")
        a2 = Article(url="https://b.com", title="B", content="B")
        assert a1.metadata is not a2.metadata

    def test_frozen_raises_on_mutation(self) -> None:
        """Article is frozen; setting any attribute must raise."""
        a = Article(url="https://example.com", title="T", content="C")
        with pytest.raises((FrozenInstanceError, AttributeError)):
            a.title = "mutated"  # type: ignore[misc]

    def test_full_construction(self) -> None:
        """All fields can be set at construction time."""
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        a = Article(
            url="https://ft.com/article/1",
            title="Markets Rally",
            content="Content here",
            author="Jane Doe",
            publish_date=dt,
            raw_html="<html/>",
            metadata={"source_domain": "ft.com"},
        )
        assert a.author == "Jane Doe"
        assert a.publish_date == dt
        assert a.raw_html == "<html/>"
        assert a.metadata["source_domain"] == "ft.com"


# ---------------------------------------------------------------------------
# ScrapeResult
# ---------------------------------------------------------------------------


class TestScrapeResult:
    def test_minimal_construction(self) -> None:
        """ScrapeResult constructs with only the two required fields."""
        r = ScrapeResult(status="success", driver_used="curl_cffi")
        assert r.status == "success"
        assert r.driver_used == "curl_cffi"

    def test_field_ordering_regression(self) -> None:
        """Regression: non-default fields first — bad ordering raises TypeError at import."""
        # If field ordering is wrong, the dataclass raises TypeError before this runs.
        r = ScrapeResult(status="success", driver_used="curl_cffi")
        assert r is not None

    def test_defaults(self) -> None:
        """All defaulted fields must start at their documented defaults."""
        r = ScrapeResult(status="error", driver_used="primp")
        assert r.article is None
        assert r.error is None
        assert r.proxy_used is None
        assert r.challenge_solved is False
        assert r.retry_count == 0
        assert r.fetch_duration_ms == 0

    def test_frozen_raises_on_mutation(self) -> None:
        """ScrapeResult is frozen; mutation must raise."""
        r = ScrapeResult(status="success", driver_used="curl_cffi")
        with pytest.raises((FrozenInstanceError, AttributeError)):
            r.status = "error"  # type: ignore[misc]

    def test_full_construction(self) -> None:
        """All fields can be set at construction time."""
        article = Article(url="https://example.com", title="T", content="C")
        r = ScrapeResult(
            status="challenge",
            driver_used="patchright",
            article=article,
            error=None,
            proxy_used="http://proxy:3128",
            challenge_solved=True,
            retry_count=2,
            fetch_duration_ms=1500,
        )
        assert r.article is article
        assert r.challenge_solved is True
        assert r.retry_count == 2
        assert r.fetch_duration_ms == 1500


# ---------------------------------------------------------------------------
# BrowserProfile
# ---------------------------------------------------------------------------


class TestBrowserProfile:
    def _make_profile(self, **overrides) -> BrowserProfile:
        defaults = {
            "name": "chrome_win",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131",
            "tls_fingerprint": "ja4:abc123",
            "http2_settings": {"HEADER_TABLE_SIZE": 65536},
            "platform": "Win32",
            "chrome_major_version": 131,
        }
        defaults.update(overrides)
        return BrowserProfile(**defaults)

    def test_minimal_construction(self) -> None:
        """BrowserProfile constructs with required fields only."""
        p = self._make_profile()
        assert p.name == "chrome_win"
        assert p.chrome_major_version == 131

    def test_accept_language_default(self) -> None:
        """accept_language defaults to 'en-US,en;q=0.9'."""
        p = self._make_profile()
        assert p.accept_language == "en-US,en;q=0.9"

    def test_viewport_default(self) -> None:
        """viewport defaults to (1920, 1080)."""
        p = self._make_profile()
        assert p.viewport == (1920, 1080)

    def test_frozen_raises_on_mutation(self) -> None:
        """BrowserProfile is frozen; mutation must raise."""
        p = self._make_profile()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            p.platform = "MacIntel"  # type: ignore[misc]

    def test_custom_viewport(self) -> None:
        """viewport can be overridden at construction."""
        p = self._make_profile(viewport=(2560, 1440))
        assert p.viewport == (2560, 1440)


# ---------------------------------------------------------------------------
# ProxySession
# ---------------------------------------------------------------------------


class TestProxySession:
    def test_minimal_construction(self) -> None:
        """ProxySession constructs with only the url field."""
        ps = ProxySession(url="http://user:pass@proxy.example.com:3128")
        assert ps.url == "http://user:pass@proxy.example.com:3128"

    def test_defaults(self) -> None:
        """All optional fields start at their documented defaults."""
        ps = ProxySession(url="http://proxy:3128")
        assert ps.health_status == "unknown"
        assert ps.last_used is None
        assert ps.failure_count == 0
        assert ps.assigned_scraper is None
        assert ps.country_code is None

    def test_health_status_is_mutable(self) -> None:
        """ProxySession is NOT frozen; health_status can be changed after construction."""
        ps = ProxySession(url="http://proxy:3128")
        ps.health_status = "healthy"
        assert ps.health_status == "healthy"

    def test_failure_count_is_mutable(self) -> None:
        """failure_count can be incremented on an existing instance."""
        ps = ProxySession(url="http://proxy:3128")
        ps.failure_count += 1
        assert ps.failure_count == 1

    def test_mutation_persists(self) -> None:
        """Mutations are visible on the same reference (not a copy)."""
        ps = ProxySession(url="http://proxy:3128")
        ps.health_status = "burned"
        ps.failure_count = 5
        # Read back via the same variable — value must persist.
        assert ps.health_status == "burned"
        assert ps.failure_count == 5


# ---------------------------------------------------------------------------
# StorageState
# ---------------------------------------------------------------------------


class TestStorageState:
    def test_minimal_construction(self) -> None:
        """StorageState constructs with only the domain field."""
        s = StorageState(domain="ft.com")
        assert s.domain == "ft.com"

    def test_collection_defaults(self) -> None:
        """cookies, local_storage, and session_storage default to empty collections."""
        s = StorageState(domain="ft.com")
        assert s.cookies == []
        assert s.local_storage == {}
        assert s.session_storage == {}

    def test_boolean_defaults(self) -> None:
        """is_valid defaults to True; expires_at defaults to None."""
        s = StorageState(domain="ft.com")
        assert s.is_valid is True
        assert s.expires_at is None

    def test_created_at_is_timezone_aware(self) -> None:
        """created_at must be UTC-aware (not just any tz, and never a naive datetime)."""
        s = StorageState(domain="ft.com")
        assert s.created_at.tzinfo is not None, (
            "created_at must be timezone-aware; datetime.utcnow() is deprecated and forbidden"
        )
        assert s.created_at.utcoffset() == timedelta(0), "created_at must be UTC, not local tz"

    def test_collection_defaults_are_independent(self) -> None:
        """Two StorageState instances must NOT share the same list/dict objects."""
        s1 = StorageState(domain="a.com")
        s2 = StorageState(domain="b.com")
        assert s1.cookies is not s2.cookies
        assert s1.local_storage is not s2.local_storage
        assert s1.session_storage is not s2.session_storage

    def test_mutable_state(self) -> None:
        """StorageState is NOT frozen; is_valid can be set to False after construction."""
        s = StorageState(domain="ft.com")
        s.is_valid = False
        assert s.is_valid is False
