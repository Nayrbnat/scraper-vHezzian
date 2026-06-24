"""make_engine enables SSL (for Neon) only when DATABASE_SSL is set — local stays plain."""

from __future__ import annotations


def test_ssl_connect_args_opt_in(monkeypatch) -> None:
    from scrapeforge.core.db.session import _ssl_connect_args

    monkeypatch.delenv("DATABASE_SSL", raising=False)
    assert _ssl_connect_args() == {}

    monkeypatch.setenv("DATABASE_SSL", "require")
    assert _ssl_connect_args() == {"ssl": True}

    monkeypatch.setenv("DATABASE_SSL", "false")
    assert _ssl_connect_args() == {}


def test_make_engine_uses_ssl_args(monkeypatch) -> None:
    from scrapeforge.core.db import session as sess

    captured = {}

    def _fake_create(url, **kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(sess, "create_async_engine", _fake_create)
    monkeypatch.setenv("DATABASE_SSL", "require")
    sess.make_engine("postgresql+asyncpg://u:p@h/db")
    assert captured.get("connect_args") == {"ssl": True}
