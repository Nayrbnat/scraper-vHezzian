"""DigestSettings ranking knobs (defaults + env override)."""

from __future__ import annotations


def test_defaults(fake_env) -> None:
    from scrapeforge.digest.settings import DigestSettings

    s = DigestSettings()
    assert s.DIGEST_RELEVANCE_FLOOR == 5
    assert s.DIGEST_TOP_N == 10
    assert s.DIGEST_WINDOW_HOURS == 48


def test_env_override(monkeypatch, fake_env) -> None:
    from scrapeforge.digest.settings import DigestSettings

    monkeypatch.setenv("DIGEST_RELEVANCE_FLOOR", "7")
    monkeypatch.setenv("DIGEST_TOP_N", "5")
    monkeypatch.setenv("DIGEST_WINDOW_HOURS", "24")
    s = DigestSettings()
    assert (s.DIGEST_RELEVANCE_FLOOR, s.DIGEST_TOP_N, s.DIGEST_WINDOW_HOURS) == (7, 5, 24)
