"""CLI wiring for `pipeline ingest-news` (job mocked; no network, no DB)."""

from __future__ import annotations

from typer.testing import CliRunner

from scrapeforge.pipeline.cli import pipeline_app

runner = CliRunner()

_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA="


def test_ingest_news_registered() -> None:
    result = runner.invoke(pipeline_app, ["--help"])
    assert result.exit_code == 0
    assert "ingest-news" in result.stdout


def test_ingest_news_runs(monkeypatch) -> None:
    monkeypatch.setenv("STATE_STORE_KEY", _KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")

    async def _fake(**kwargs):  # noqa: ANN003, ARG001
        return 9

    monkeypatch.setattr("scrapeforge.pipeline.jobs.ingest_news_feeds", _fake)
    result = runner.invoke(pipeline_app, ["ingest-news", "--max", "2", "--limit", "5"])
    assert result.exit_code == 0, result.stdout
    assert "9" in result.stdout
