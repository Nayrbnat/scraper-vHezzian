"""render.yaml is valid, defines the cron jobs, and carries NO inline secret values."""

from __future__ import annotations

from pathlib import Path

import yaml

_SECRET_NAMES = {
    "DATABASE_URL",
    "STATE_STORE_KEY",
    "SUMMARY_API_KEY",
    "DIGEST_SMTP_PASSWORD",
    "DIGEST_SMTP_USER",
    "DIGEST_TO",
}


def test_render_yaml_valid_and_secretless() -> None:
    doc = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    services = doc["services"]
    names = {s["name"] for s in services}
    # the three scheduled jobs + init-db are present
    assert {"ingest", "summarize", "digest"} <= names
    for svc in services:
        assert svc["type"] == "cron"
        for ev in svc.get("envVars", []):
            # secrets must be declared by name only (sync: false), never an inline value
            if ev["key"] in _SECRET_NAMES:
                assert ev.get("sync") is False, f"{ev['key']} must be sync:false (dashboard secret)"
                assert "value" not in ev, f"{ev['key']} must NOT have an inline value"
