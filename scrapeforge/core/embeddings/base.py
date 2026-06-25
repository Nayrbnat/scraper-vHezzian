"""The Embedder port: turn a list of texts into a list of equal-length vectors."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Embedder(ABC):
    """Provider-agnostic boundary the embedding jobs depend on."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order.

        Implementations batch internally. ``len(result) == len(texts)`` and every
        vector has the same dimension (``EMBED_DIM``). An empty input returns ``[]``.
        """
