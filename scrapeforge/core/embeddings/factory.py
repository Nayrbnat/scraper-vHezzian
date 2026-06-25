"""Select an Embedder adapter by ``EMBED_PROVIDER`` (extension by addition)."""

from __future__ import annotations

from scrapeforge.core.embeddings.base import Embedder
from scrapeforge.core.embeddings.gemini import GeminiEmbedder
from scrapeforge.core.embeddings.openai_compatible import OpenAICompatibleEmbedder
from scrapeforge.core.embeddings.settings import EmbedderSettings


def make_embedder(settings: EmbedderSettings) -> Embedder:
    """Return the embedder adapter named by ``settings.EMBED_PROVIDER``."""
    provider = settings.EMBED_PROVIDER.strip().lower()
    if provider == "gemini":
        return GeminiEmbedder(settings)
    if provider == "openai_compatible":
        return OpenAICompatibleEmbedder(settings)
    raise ValueError(f"unknown EMBED_PROVIDER: {settings.EMBED_PROVIDER!r}")
