"""Unit tests for finalize_vector (dim guard + L2 normalization)."""

from __future__ import annotations

import math

import pytest

from scrapeforge.core.embeddings._vectors import finalize_vector
from scrapeforge.core.embeddings.exceptions import EmbeddingParseError


def test_normalizes_to_unit_length() -> None:
    out = finalize_vector([3.0, 4.0], expected_dim=2)
    assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, rel_tol=1e-9)
    assert math.isclose(out[0], 0.6) and math.isclose(out[1], 0.8)


def test_already_unit_vector_unchanged() -> None:
    out = finalize_vector([1.0, 0.0, 0.0], expected_dim=3)
    assert out == [1.0, 0.0, 0.0]


def test_wrong_dimension_raises() -> None:
    with pytest.raises(EmbeddingParseError, match="dimension"):
        finalize_vector([1.0, 0.0], expected_dim=3)


def test_zero_vector_returned_unchanged() -> None:
    assert finalize_vector([0.0, 0.0], expected_dim=2) == [0.0, 0.0]
