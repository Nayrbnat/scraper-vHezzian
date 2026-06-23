"""Tests for the per-module SummarizerSettings fragment (CSV parsing + defaults)."""

from __future__ import annotations


def test_defaults(fake_env) -> None:
    from scrapeforge.core.llm.settings import SummarizerSettings

    s = SummarizerSettings()
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
