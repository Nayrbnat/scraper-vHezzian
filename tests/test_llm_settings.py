"""Tests for the per-module SummarizerSettings fragment (CSV parsing + defaults)."""

from __future__ import annotations


def test_defaults(monkeypatch, fake_env) -> None:
    from scrapeforge.core.llm.settings import SummarizerSettings

    # Hermetic: ignore any real .env / OS env so we test the code's factory defaults,
    # not the developer's local configuration.
    for key in (
        "SUMMARY_MODEL",
        "SUMMARY_BATCH_SIZE",
        "SUMMARY_API_KEY",
        "SUMMARY_API_BASE_URL",
        "SUMMARY_PORTFOLIO",
        "SUMMARY_INTERESTS",
    ):
        monkeypatch.delenv(key, raising=False)
    s = SummarizerSettings(_env_file=None)
    assert s.SUMMARY_MODEL == "glm-4.5-flash"
    assert s.SUMMARY_BATCH_SIZE == 20
    assert s.SUMMARY_API_KEY == ""
    assert "z.ai" in s.SUMMARY_API_BASE_URL
    assert s.portfolio() == []
    assert s.interests() == []


def test_csv_parsing(monkeypatch, fake_env) -> None:
    from scrapeforge.core.llm.settings import SummarizerSettings

    monkeypatch.setenv("SUMMARY_PORTFOLIO", "Nvidia, TSMC ,, Anthropic")
    monkeypatch.setenv("SUMMARY_INTERESTS", "hybrid bonding, SpaceX IPO")
    s = SummarizerSettings()
    assert s.portfolio() == ["Nvidia", "TSMC", "Anthropic"]
    assert s.interests() == ["hybrid bonding", "SpaceX IPO"]
