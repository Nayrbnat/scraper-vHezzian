"""CLI wiring for `pipeline sync-users` (job mocked; no DB, no network)."""

from __future__ import annotations

from typer.testing import CliRunner

from scrapeforge.pipeline.cli import pipeline_app

runner = CliRunner()

_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA="


def test_sync_users_registered() -> None:
    result = runner.invoke(pipeline_app, ["--help"])
    assert result.exit_code == 0
    assert "sync-users" in result.stdout


def test_sync_users_skips_without_url(monkeypatch) -> None:
    monkeypatch.setenv("HEZZIAN_DATABASE_URL", "")
    result = runner.invoke(pipeline_app, ["sync-users"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout.lower()


def test_sync_users_runs_with_url(monkeypatch) -> None:
    monkeypatch.setenv("HEZZIAN_DATABASE_URL", "postgresql://u:p@h/hezzian")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/scraper_news")
    # Settings() is built inside the command; CI has no .env, so supply the throwaway key.
    monkeypatch.setenv("STATE_STORE_KEY", _KEY)
    monkeypatch.setattr("scrapeforge.pipeline.user_sync.run_sync_sync", lambda *a, **k: 3)

    result = runner.invoke(pipeline_app, ["sync-users"])
    assert result.exit_code == 0, result.stdout
    assert "3" in result.stdout
