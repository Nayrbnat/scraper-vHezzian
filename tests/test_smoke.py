"""Smoke tests — prove the harness itself works before any product code exists.

Replace/extend these as real modules land (see TESTING.md for the per-module test matrix).
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def test_repo_layout_is_present() -> None:
    """The foundational docs/config exist at the repo root."""
    root = Path(__file__).resolve().parents[1]
    for name in ("pyproject.toml", "SPEC.md", "architecture.MD", "CLAUDE.md", "GitHub.md"):
        assert (root / name).is_file(), f"missing {name}"


def test_async_harness_runs() -> None:
    """An event loop runs (the whole codebase is async-first). Plugin-free on purpose so smoke
    stays green everywhere; `asyncio_mode = auto` collection is exercised by the real async unit
    tests added per TESTING.md (they require the pytest-asyncio extra, installed in CI)."""

    async def _coro() -> int:
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(_coro()) == 42


def test_fake_env_fixture(fake_env: dict[str, str]) -> None:
    """The shared fake_env fixture is wired and never leaks real secrets."""
    import os

    assert os.environ["STATE_STORE_KEY"] == fake_env["STATE_STORE_KEY"]
    assert len(fake_env["STATE_STORE_KEY"]) >= 32
