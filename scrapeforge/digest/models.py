"""Digest domain models (Pydantic v2) — the forward-compatible contract.

These are the schemas the whole feature works against, from prototype through production:

- ``Subscriber``        — who we email (captured at Hezzian signup: name + email + preferences).
- ``DigestPreferences`` — the "what do you want updated on?" answers that personalize the digest.
- ``DigestItem``        — one piece of content (an article matched to a preference).
- ``DigestSection``     — a titled group of items (e.g. "Your portfolio companies").
- ``Digest``            — the assembled, personalized payload that gets rendered + emailed.

JSON in/out is first-class: ``Subscriber.model_validate_json(...)`` loads a seeded individual,
and ``Digest.model_dump_json()`` serializes the rendered digest (useful for previews/snapshots).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Cadence = Literal["daily", "weekly"]


class DigestPreferences(BaseModel):
    """A subscriber's content preferences — populated from the signup questionnaire.

    Everything is matched case-insensitively against article title + content (prototype:
    deterministic keyword matching; a later phase adds semantic/pgvector relevance).
    """

    model_config = ConfigDict(extra="forbid")

    portfolio_companies: list[str] = Field(
        default_factory=list,
        description="Companies the investor holds / tracks, e.g. ['Stripe', 'Anthropic'].",
    )
    investment_themes: list[str] = Field(
        default_factory=list,
        description="Macro/sector themes, e.g. ['AI infrastructure', 'fintech', 'defense tech'].",
    )
    news_topics: list[str] = Field(
        default_factory=list,
        description="Broader interests, e.g. ['funding rounds', 'M&A', 'regulation'].",
    )
    data_types: list[str] = Field(
        default_factory=lambda: ["news"],
        # Collected at signup; not yet used in matching (forward-compat placeholder for when the
        # corpus carries typed records like earnings/filings).
        description="Kinds of data wanted, e.g. ['news', 'earnings', 'filings'].",
    )
    cadence: Cadence = "daily"
    max_items_per_section: int = Field(default=5, ge=1, le=25)


class Subscriber(BaseModel):
    """An individual who receives Hezzian digests.

    At signup Hezzian captures ``name`` + ``email``; the questionnaire fills ``preferences``.
    ``id`` links the whole account (forward-compatible with a future users table — here it is
    just a stable slug for the seeded prototype individual).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable account id / slug (links the whole account).")
    name: str
    email: EmailStr
    preferences: DigestPreferences = Field(default_factory=DigestPreferences)
    # Forward-compat fields a real digest scheduler needs (cheap to add now, painful to backfill):
    active: bool = Field(default=True, description="Whether to send digests to this subscriber.")
    timezone: str | None = Field(
        default=None, description="IANA tz, e.g. 'Europe/London' (send-time)."
    )
    created_at: datetime | None = None


class DigestItem(BaseModel):
    """One matched article in a digest section."""

    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    source: str = Field(description="Source domain, e.g. 'reuters.com'.")
    published: datetime | None = None
    summary: str = Field(description="Short summary shown in the email.")
    matched_on: list[str] = Field(
        default_factory=list,
        description="Which preference values this item matched (company/theme/topic).",
    )


class DigestSection(BaseModel):
    """A titled group of items, e.g. portfolio companies or investment themes."""

    model_config = ConfigDict(extra="forbid")

    key: Literal["portfolio", "themes", "topics"]
    heading: str
    items: list[DigestItem] = Field(default_factory=list)


class Digest(BaseModel):
    """The assembled, personalized digest for one subscriber on one period."""

    model_config = ConfigDict(extra="forbid")

    subscriber_id: str
    subscriber_name: str
    subscriber_email: EmailStr
    cadence: Cadence = "daily"
    period: str = Field(description="Human period label, e.g. '2026-06-22' or 'Week of ...'.")
    generated_at: datetime
    sections: list[DigestSection] = Field(default_factory=list)

    @property
    def cadence_label(self) -> str:
        return "Daily" if self.cadence == "daily" else "Weekly"

    @property
    def total_items(self) -> int:
        return sum(len(s.items) for s in self.sections)

    @property
    def is_empty(self) -> bool:
        return self.total_items == 0
