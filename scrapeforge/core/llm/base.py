"""The Summarizer port: produce a 5-bullet summary + 1-10 relevance score for one article."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """The structured output of one summarize call.

    Attributes:
        bullets:   3-5 non-empty investor bullets (target 5; normalized by the adapter).
        relevance: overall 1-10 relevance-to-the-owner score.
        scores:    per-criterion 1-10 sub-scores
                   ({relevance, credibility, intensity, personal, time}).
        reason:    one-line rationale for the score.
        model:     the model id that produced this result.
    """

    bullets: list[str]
    relevance: int
    scores: dict[str, int]
    reason: str
    model: str


class Summarizer(ABC):
    """Provider-agnostic boundary the summarize worker depends on."""

    @abstractmethod
    async def summarize(
        self,
        *,
        title: str,
        content: str,
        published: datetime | None,
        portfolio: list[str],
        interests: list[str],
    ) -> SummaryResult:
        """Summarize + score one article for an investor with *portfolio*/*interests*."""
