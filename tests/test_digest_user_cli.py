"""CLI wiring for the per-user digest commands (loader mocked; no network, no DB)."""

from __future__ import annotations

from typer.testing import CliRunner

from scrapeforge.digest.cli import digest_app

runner = CliRunner()

_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA="


def test_user_commands_registered() -> None:
    result = runner.invoke(digest_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("preview-all", "send-all"):
        assert cmd in result.stdout


def test_preview_all_runs_with_no_users(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STATE_STORE_KEY", _KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setattr("scrapeforge.digest.user_source.load_all_sync", lambda *a, **k: [])

    result = runner.invoke(digest_app, ["preview-all", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "sent=0 skipped_empty=0 failed=0" in result.stdout


def test_send_all_aborts_without_yes(monkeypatch) -> None:
    monkeypatch.setenv("STATE_STORE_KEY", _KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")
    # No --yes and stdin "n" => typer.confirm aborts before any send is attempted.
    result = runner.invoke(digest_app, ["send-all"], input="n\n")
    assert result.exit_code != 0
