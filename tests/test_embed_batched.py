"""embed_articles_batched: paced multi-batch drain with graceful rate-limit handling (unit)."""

from __future__ import annotations

import pytest

from scrapeforge.core.embeddings.exceptions import EmbeddingRateLimitError


async def test_loops_until_drained(monkeypatch) -> None:
    from scrapeforge.pipeline import embeddings_jobs

    returns = iter([16, 16, 3, 0])

    async def _fake(**kwargs):  # noqa: ANN003, ARG001
        return next(returns)

    monkeypatch.setattr(embeddings_jobs, "embed_articles", _fake)
    total = await embeddings_jobs.embed_articles_batched(
        session_factory=None, embedder=None, batch_size=16, max_batches=10, pause_seconds=0
    )
    assert total == 35  # 16 + 16 + 3, then a 0-batch stops it


async def test_stops_gracefully_on_rate_limit(monkeypatch) -> None:
    from scrapeforge.pipeline import embeddings_jobs

    seq = [16, 16]

    async def _fake(**kwargs):  # noqa: ANN003, ARG001
        if seq:
            return seq.pop(0)
        raise EmbeddingRateLimitError("HTTP 429")

    monkeypatch.setattr(embeddings_jobs, "embed_articles", _fake)
    # Must NOT raise — the run stays green and returns what it managed to embed.
    total = await embeddings_jobs.embed_articles_batched(
        session_factory=None, embedder=None, batch_size=16, max_batches=10, pause_seconds=0
    )
    assert total == 32


async def test_respects_max_batches(monkeypatch) -> None:
    from scrapeforge.pipeline import embeddings_jobs

    async def _never_drains(**kwargs):  # noqa: ANN003, ARG001
        return 16

    monkeypatch.setattr(embeddings_jobs, "embed_articles", _never_drains)
    total = await embeddings_jobs.embed_articles_batched(
        session_factory=None, embedder=None, batch_size=16, max_batches=3, pause_seconds=0
    )
    assert total == 48  # capped at 3 batches × 16


def test_batched_settings_defaults(monkeypatch) -> None:
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    for key in ("EMBED_BATCH_SIZE", "EMBED_MAX_BATCHES", "EMBED_BATCH_PAUSE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    s = EmbedderSettings(_env_file=None)
    assert s.EMBED_BATCH_SIZE == 16  # small enough to stay under Gemini's free per-request limit
    assert s.EMBED_MAX_BATCHES == 25
    assert pytest.approx(1.5) == s.EMBED_BATCH_PAUSE_SECONDS
