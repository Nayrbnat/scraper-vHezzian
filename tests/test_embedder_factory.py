"""Tests for EmbedderSettings + the make_embedder factory."""

from __future__ import annotations


def test_settings_defaults_to_gemini_1536() -> None:
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    s = EmbedderSettings()
    assert s.EMBED_PROVIDER == "gemini"
    assert s.EMBED_MODEL == "gemini-embedding-001"
    assert s.EMBED_DIM == 1536
    assert s.EMBED_API_KEY == ""  # empty => jobs idle
