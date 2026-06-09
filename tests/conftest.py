"""Shared pytest fixtures.

Deliberately free of product imports for now — ScrapeForge modules don't exist yet, and importing
them here would break collection. As each module lands, add focused fixtures next to its tests (or
extend this file) per TESTING.md. These generic fixtures are safe to use immediately.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_states_dir(tmp_path: Path) -> Path:
    """An isolated, throwaway directory standing in for ~/.scrapeforge/states/."""
    d = tmp_path / "states"
    d.mkdir()
    return d


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Populate the env with safe, deterministic test config (never real secrets)."""
    values = {
        # 32+ char base64 Fernet key is required by Settings validation; this is a throwaway.
        "STATE_STORE_KEY": "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA=",
        "LOG_LEVEL": "WARNING",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values


@pytest.fixture
def frozen_clock():
    """Convenience wrapper around freezegun for time-dependent units (RateLimiter, TTLs)."""
    from freezegun import freeze_time

    return freeze_time


@pytest.fixture(autouse=True)
def _no_accidental_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Guard rail: unit tests must not hit the real network. Integration tests opt out via marker.

    This does not block anything by itself (drivers aren't imported here yet); it documents intent
    and gives a single place to wire a socket guard once the HTTP layer exists.
    """
    if "integration" in request.keywords:
        return
    # Placeholder for a future socket-blocking guard (e.g. pytest-socket).
    os.environ.setdefault("SCRAPEFORGE_OFFLINE", "1")
