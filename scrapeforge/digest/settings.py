"""Per-module ranking config for the relevance digest (Invariant #16 — never core Settings)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DigestSettings(BaseSettings):
    """Knobs for the relevance-ranked digest. Overridable via environment / ``.env``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DIGEST_RELEVANCE_FLOOR: int = Field(default=5)  # minimum 1-10 score to include
    DIGEST_TOP_N: int = Field(default=10)  # max items per email
    DIGEST_WINDOW_HOURS: int = Field(default=48)  # recency window considered
