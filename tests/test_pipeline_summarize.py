"""The summarize command is registered and idles cleanly without a key (no spend)."""

from __future__ import annotations


def test_summarize_registered() -> None:
    from typer.testing import CliRunner

    from scrapeforge.cli import app

    result = CliRunner().invoke(app, ["pipeline", "--help"])
    assert result.exit_code == 0 and "summarize" in result.stdout


def test_summarize_no_key_is_noop(fake_env, monkeypatch) -> None:
    """With an empty SUMMARY_API_KEY the command exits cleanly without constructing the LLM or DB.

    The guard returns BEFORE any engine/LLM is built, so a spy on the (lazily-imported)
    summarizer must never be called.
    """
    from typer.testing import CliRunner

    from scrapeforge.cli import app

    monkeypatch.setenv("SUMMARY_API_KEY", "")

    constructed = []
    monkeypatch.setattr(
        "scrapeforge.core.llm.openai_compatible.OpenAICompatibleSummarizer",
        lambda *a, **k: constructed.append(1),
    )

    result = CliRunner().invoke(app, ["pipeline", "summarize"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout
    assert constructed == []  # never built the LLM (early return before the lazy import)
