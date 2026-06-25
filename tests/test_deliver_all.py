"""deliver_all loops users, isolates per-user failures, skips empties, returns counts."""

from __future__ import annotations

import pytest

from scrapeforge.core.models import Article
from scrapeforge.digest.sender import EmailSender
from scrapeforge.digest.user_source import ActiveUser


class _FakeSender(EmailSender):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, to: str, email) -> None:  # noqa: ANN001
        if to == "boom@e.com":
            raise RuntimeError("smtp down")
        self.sent.append(to)


def _art() -> Article:
    return Article(
        url="https://e.com/a",
        title="t",
        content="c",
        author=None,
        publish_date=None,
        metadata={"relevance": 7, "summary": {"bullets": ["b1"], "reason": "r"}},
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, fake_env):
    # deliver_all constructs Settings() (needs STATE_STORE_KEY via fake_env) + reads DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")


def test_deliver_all_isolates_failures_and_skips_empty(monkeypatch) -> None:
    from scrapeforge.digest import service

    batches = [
        (ActiveUser("u1", "ok@e.com", "ok"), [_art()]),
        (ActiveUser("u2", "empty@e.com", "empty"), []),
        (ActiveUser("u3", "boom@e.com", "boom"), [_art()]),
    ]
    monkeypatch.setattr("scrapeforge.digest.user_source.load_all_sync", lambda *a, **k: batches)

    sender = _FakeSender()
    summary = service.deliver_all(source="postgres", sender=sender)

    assert summary.sent == 1
    assert summary.skipped_empty == 1
    assert summary.failed == 1
    assert sender.sent == ["ok@e.com"]
    assert "sent=1 skipped_empty=1 failed=1" in str(summary)


def test_deliver_all_rejects_non_postgres_source() -> None:
    from scrapeforge.digest import service

    with pytest.raises(ValueError, match="postgres"):
        service.deliver_all(source="sample")


def test_deliver_all_default_sender_is_preview(monkeypatch, tmp_path) -> None:
    from scrapeforge.digest import service
    from scrapeforge.digest.sender import PreviewEmailSender

    monkeypatch.setattr(
        "scrapeforge.digest.user_source.load_all_sync",
        lambda *a, **k: [(ActiveUser("u1", "a@e.com", "a"), [_art()])],
    )
    summary = service.deliver_all(source="postgres", sender=PreviewEmailSender(tmp_path))
    assert summary.sent == 1
    assert (tmp_path / "a_at_e_com.html").exists()
