"""Hermetic default for the user-sync settings fragment."""

from __future__ import annotations


def test_hezzian_url_default_empty(monkeypatch) -> None:
    from scrapeforge.pipeline.sync_settings import UserSyncSettings

    monkeypatch.delenv("HEZZIAN_DATABASE_URL", raising=False)
    s = UserSyncSettings(_env_file=None)
    assert s.HEZZIAN_DATABASE_URL == ""
