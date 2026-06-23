"""Contract tests for worker message TypedDicts."""

from __future__ import annotations


def test_ingest_message_has_expected_keys() -> None:
    from scrapeforge.worker.messages import IngestMessage

    assert set(IngestMessage.__annotations__) == {
        "job_id",
        "platform",
        "target",
        "bucket",
        "limit",
    }
