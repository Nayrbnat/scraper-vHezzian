"""CLI wiring for the embedding subcommands (job mocked; no network, no DB I/O)."""

from __future__ import annotations

from typer.testing import CliRunner

from scrapeforge.pipeline.cli import pipeline_app

runner = CliRunner()

_DSN = "postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge"


def test_embed_articles_skips_without_key(monkeypatch) -> None:
    monkeypatch.setenv("EMBED_API_KEY", "")
    result = runner.invoke(pipeline_app, ["embed-articles"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout.lower()


def test_embed_articles_runs_with_key(monkeypatch) -> None:
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("DATABASE_URL", _DSN)

    called = {}

    async def _fake_job(**kwargs):
        called["ran"] = True
        return 7

    monkeypatch.setattr("scrapeforge.pipeline.embeddings_jobs.embed_articles", _fake_job)
    result = runner.invoke(pipeline_app, ["embed-articles"])
    assert result.exit_code == 0, result.stdout
    assert called.get("ran") is True
    assert "7" in result.stdout


def test_score_users_command_registered() -> None:
    result = runner.invoke(pipeline_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("seed-owner", "embed-articles", "embed-profiles", "score-users"):
        assert cmd in result.stdout
