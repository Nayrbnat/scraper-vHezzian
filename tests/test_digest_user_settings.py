"""Hermetic defaults for the per-user digest knobs (ignore the developer's .env)."""

from __future__ import annotations


def test_user_digest_defaults(monkeypatch) -> None:
    from scrapeforge.digest.settings import DigestSettings

    for key in ("DIGEST_USER_TOP_N", "DIGEST_USER_WINDOW_HOURS", "DIGEST_USER_SCORE_FLOOR"):
        monkeypatch.delenv(key, raising=False)
    s = DigestSettings(_env_file=None)
    assert s.DIGEST_USER_TOP_N == 10
    assert s.DIGEST_USER_WINDOW_HOURS == 48
    assert s.DIGEST_USER_SCORE_FLOOR == 0.0
