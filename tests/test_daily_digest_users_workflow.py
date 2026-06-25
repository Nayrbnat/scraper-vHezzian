"""The per-user delivery workflow exists and runs the send-all command with SMTP secrets."""

from __future__ import annotations

from pathlib import Path


def test_workflow_runs_send_all() -> None:
    text = Path(".github/workflows/daily-digest-users.yml").read_text(encoding="utf-8")
    assert "digest send-all" in text
    assert "--yes" in text
    assert "DIGEST_SMTP_USER" in text
    assert "DIGEST_SMTP_PASSWORD" in text
    assert "DATABASE_URL" in text
    assert "workflow_dispatch" in text
