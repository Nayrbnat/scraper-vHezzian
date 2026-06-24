"""The free GitHub Actions deploy path runs the pipeline against Neon over TLS, secret-safe.

This is the card-free alternative to ``render.yaml``: GitHub Actions (free scheduled compute)
drives ``init-db -> ingest -> summarize`` into a managed Neon Postgres, and the existing
``daily-digest.yml`` sends the relevance email from the same DB. These guards keep both
workflows TLS-correct (asyncpg needs SSL to Neon) and free of inlined secrets.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PIPELINE = Path(".github/workflows/daily-pipeline.yml")
DIGEST = Path(".github/workflows/daily-digest.yml")


def test_daily_pipeline_is_valid_yaml_and_scheduled() -> None:
    doc = yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))
    # PyYAML (YAML 1.1) parses the ``on:`` key as the boolean True.
    triggers = doc.get("on", doc.get(True))
    assert triggers is not None, "workflow must declare triggers"
    assert "schedule" in triggers, "must run on a free GitHub cron schedule"
    assert "workflow_dispatch" in triggers, "must allow manual runs for the first live run"


def test_daily_pipeline_runs_init_ingest_summarize_in_order() -> None:
    text = PIPELINE.read_text(encoding="utf-8")
    assert "pipeline init-db" in text
    assert "pipeline ingest" in text
    assert "pipeline summarize" in text
    # idempotent init first, then scrape, then score the new rows
    assert (
        text.index("pipeline init-db")
        < text.index("pipeline ingest")
        < text.index("pipeline summarize")
    ), "must run init-db -> ingest -> summarize in order"


def test_daily_pipeline_uses_tls_to_neon() -> None:
    text = PIPELINE.read_text(encoding="utf-8")
    assert "DATABASE_SSL: require" in text, "asyncpg needs TLS to reach Neon"


def test_daily_pipeline_secrets_are_referenced_not_inlined() -> None:
    text = PIPELINE.read_text(encoding="utf-8")
    for name in ("DATABASE_URL", "SUMMARY_API_KEY"):
        assert f"secrets.{name}" in text, f"{name} must come from a GitHub secret, never inlined"


def test_daily_digest_uses_tls_to_neon() -> None:
    # the email path reads the same Neon DB on the postgres source, so it also needs TLS
    assert "DATABASE_SSL: require" in DIGEST.read_text(encoding="utf-8")
