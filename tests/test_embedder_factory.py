"""Tests for EmbedderSettings + the make_embedder factory."""

from __future__ import annotations


def test_settings_defaults_to_gemini_1536() -> None:
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    s = EmbedderSettings()
    assert s.EMBED_PROVIDER == "gemini"
    assert s.EMBED_MODEL == "gemini-embedding-001"
    assert s.EMBED_DIM == 1536
    assert s.EMBED_API_KEY == ""  # empty => jobs idle


def test_factory_picks_gemini() -> None:
    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.gemini import GeminiEmbedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    embedder = make_embedder(EmbedderSettings(EMBED_PROVIDER="gemini", EMBED_API_KEY="k"))
    assert isinstance(embedder, GeminiEmbedder)


def test_factory_picks_openai_compatible() -> None:
    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    embedder = make_embedder(
        EmbedderSettings(EMBED_PROVIDER="openai_compatible", EMBED_API_KEY="k")
    )
    assert isinstance(embedder, OpenAICompatibleEmbedder)


def test_factory_rejects_unknown_provider() -> None:
    import pytest

    from scrapeforge.core.embeddings.factory import make_embedder
    from scrapeforge.core.embeddings.settings import EmbedderSettings

    with pytest.raises(ValueError, match="EMBED_PROVIDER"):
        make_embedder(EmbedderSettings(EMBED_PROVIDER="nope", EMBED_API_KEY="k"))
