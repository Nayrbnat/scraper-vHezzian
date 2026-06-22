"""Hezzian digest — personalized email updates for an investor.

Consumes the scraped ``Article`` corpus and produces a per-subscriber digest (their portfolio
companies + investment themes + topics), rendered to HTML/text and delivered via a pluggable
``EmailSender`` (preview by default; SMTP for real delivery).

PROTOTYPE scope: one individual, seeded from a JSON file (``data/subscribers/<name>.json``) — no
user database yet. The Pydantic models here are the forward-compatible contract: at signup we
capture name + email, then the "what do you want updated on?" answers fill ``DigestPreferences``.
"""

from scrapeforge.digest.models import (
    Digest,
    DigestItem,
    DigestPreferences,
    DigestSection,
    Subscriber,
)

__all__ = [
    "Digest",
    "DigestItem",
    "DigestPreferences",
    "DigestSection",
    "Subscriber",
]
