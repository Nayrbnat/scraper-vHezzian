"""Tests for scrapeforge.core.registry (SPEC.md §3.16, Invariant #16).

TDD: these tests are written before the implementation.
"""

from __future__ import annotations

import pytest

from scrapeforge.core import registry as reg_module
from scrapeforge.core.registry import get_scraper_for, register_scraper

# ---------------------------------------------------------------------------
# Fixture: isolate global _REGISTRY between every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot _REGISTRY before each test; restore it after."""
    snapshot = dict(reg_module._REGISTRY)
    yield
    reg_module._REGISTRY.clear()
    reg_module._REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# Dummy scraper stubs (no real driver imports needed)
# ---------------------------------------------------------------------------


class _FakeScraperA:
    """Stub class A for registration tests."""


class _FakeScraperB:
    """Stub class B — different from A."""


# ---------------------------------------------------------------------------
# register_scraper
# ---------------------------------------------------------------------------


def test_register_scraper_binds_single_domain():
    """Decorator registers the class under the given domain."""
    register_scraper("example.com")(_FakeScraperA)
    assert reg_module._REGISTRY["example.com"] is _FakeScraperA


def test_register_scraper_binds_multiple_domains():
    """Decorator can bind the same class to several domains at once."""
    register_scraper("ft.com", "www.ft.com")(_FakeScraperA)
    assert reg_module._REGISTRY["ft.com"] is _FakeScraperA
    assert reg_module._REGISTRY["www.ft.com"] is _FakeScraperA


def test_register_scraper_returns_class_unchanged():
    """Decorator is transparent: it returns exactly the decorated class."""
    result = register_scraper("pass-through.com")(_FakeScraperA)
    assert result is _FakeScraperA


def test_register_scraper_duplicate_same_class_is_idempotent():
    """Re-registering the SAME class for a domain already bound to it is a no-op."""
    register_scraper("idempotent.com")(_FakeScraperA)
    # Should not raise:
    register_scraper("idempotent.com")(_FakeScraperA)
    assert reg_module._REGISTRY["idempotent.com"] is _FakeScraperA


def test_register_scraper_duplicate_different_class_raises():
    """Binding a DIFFERENT class to an already-claimed domain raises ValueError."""
    register_scraper("conflict.com")(_FakeScraperA)
    with pytest.raises(ValueError, match="conflict.com"):
        register_scraper("conflict.com")(_FakeScraperB)


# ---------------------------------------------------------------------------
# get_scraper_for — exact match
# ---------------------------------------------------------------------------


def test_get_scraper_for_exact_match():
    """Exact domain string returns the bound class."""
    register_scraper("exact.com")(_FakeScraperA)
    assert get_scraper_for("exact.com") is _FakeScraperA


def test_get_scraper_for_no_match_returns_none():
    """Unknown domain returns None so the engine can fall back to PublicScraper."""
    # Ensure the registry is clean for this domain:
    assert get_scraper_for("not-registered-xyz123.com") is None


# ---------------------------------------------------------------------------
# get_scraper_for — suffix match
# ---------------------------------------------------------------------------


def test_get_scraper_for_suffix_match():
    """www.ft.com resolves a registration keyed to 'ft.com'."""
    register_scraper("ft.com")(_FakeScraperA)
    assert get_scraper_for("www.ft.com") is _FakeScraperA


def test_get_scraper_for_exact_wins_over_suffix():
    """If both 'www.example.com' and 'example.com' are registered, exact match wins."""
    register_scraper("example.com")(_FakeScraperA)
    register_scraper("www.example.com")(_FakeScraperB)
    assert get_scraper_for("www.example.com") is _FakeScraperB


def test_get_scraper_for_suffix_match_requires_boundary():
    """'notexample.com' must NOT match a registration for 'example.com'."""
    register_scraper("example.com")(_FakeScraperA)
    assert get_scraper_for("notexample.com") is None


# ---------------------------------------------------------------------------
# discover_scrapers — smoke test (idempotent / importable)
# ---------------------------------------------------------------------------


def test_discover_scrapers_is_idempotent():
    """Calling discover_scrapers() twice must not raise or corrupt the registry."""
    from scrapeforge.core.registry import discover_scrapers

    snapshot_before = dict(reg_module._REGISTRY)
    discover_scrapers()
    discover_scrapers()  # second call must be safe
    # Registry may have grown (if scrapers exist); it should not have shrunk.
    for domain, cls in snapshot_before.items():
        assert reg_module._REGISTRY.get(domain) is cls


def test_register_scraper_dup_different_class_raises_value_error_directly():
    """register_scraper raises ValueError for dup-different-class via the decorator path.

    This is the same mechanism that discover_scrapers must NOT swallow: only
    ImportError (missing optional deps) should be caught and logged; a real
    registration conflict must propagate so a duplicate is caught at startup.
    """
    register_scraper("dup-direct.com")(_FakeScraperA)
    with pytest.raises(ValueError, match="dup-direct.com"):
        register_scraper("dup-direct.com")(_FakeScraperB)


def test_discover_scrapers_does_not_swallow_import_errors(monkeypatch):
    """ImportError from a bad module is logged and skipped, not re-raised.

    We monkeypatch importlib.import_module so we can simulate an ImportError
    without needing a real broken module on disk.  discover_scrapers() must
    complete without raising.
    """
    import importlib as _importlib

    import scrapeforge.core.registry as _reg

    # Reset so discover runs again after we patch.
    original_discovered = _reg._discovered
    _reg._discovered = False

    real_import = _importlib.import_module

    def _fake_import(name, *args, **kwargs):
        # Raise ImportError for any scrapers sub-module to simulate a broken dep.
        if name.startswith("scrapeforge.scrapers."):
            raise ImportError(f"simulated missing dep for {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_importlib, "import_module", _fake_import)
    try:
        # Must not raise:
        _reg.discover_scrapers()
    finally:
        _reg._discovered = original_discovered


def test_discover_scrapers_propagates_value_error_from_dup_registration(monkeypatch):
    """discover_scrapers must NOT swallow a ValueError from a duplicate registration.

    If a module triggers register_scraper for a domain already bound to a different
    class, that ValueError must propagate — it indicates a real conflict between two
    agents, and silencing it at startup would hide the bug.

    We monkeypatch importlib.import_module to simulate a scraper module that raises
    ValueError (i.e. it calls register_scraper on an already-claimed domain with a
    different class).
    """
    import importlib as _importlib

    import scrapeforge.core.registry as _reg

    original_discovered = _reg._discovered
    _reg._discovered = False

    real_import = _importlib.import_module

    def _fake_import(name, *args, **kwargs):
        if name.startswith("scrapeforge.scrapers."):
            raise ValueError("duplicate scraper registration for 'collision.com'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_importlib, "import_module", _fake_import)
    try:
        with pytest.raises(ValueError, match="collision.com"):
            _reg.discover_scrapers()
    finally:
        _reg._discovered = original_discovered
