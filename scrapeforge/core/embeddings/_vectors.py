"""Shared post-processing for embedding vectors: dimension check + L2 normalization.

Providers differ on normalization (Gemini's truncated <3072-dim output is NOT unit-normalized;
Jina's is). Normalizing here gives the port a single contract — every stored vector is unit length
— so cosine ranking and any future inner-product use agree, and per-article scores are comparable.
"""

from __future__ import annotations

import math

from scrapeforge.core.embeddings.exceptions import EmbeddingParseError


def finalize_vector(values: list[float], expected_dim: int) -> list[float]:
    """Validate length == ``expected_dim`` and return the L2-normalized vector.

    Raises ``EmbeddingParseError`` if the provider returned the wrong dimension (fail
    typed-and-early rather than at the pgvector write). A zero vector is returned unchanged
    (no division by zero).
    """
    if len(values) != expected_dim:
        raise EmbeddingParseError(f"embedding dimension {len(values)} != expected {expected_dim}")
    norm = math.sqrt(sum(x * x for x in values))
    if norm == 0.0:
        return values
    return [x / norm for x in values]
