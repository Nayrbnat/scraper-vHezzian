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

    # --- per-user delivery (Phase 3.5); ranking is by user_article_relevance cosine ---
    DIGEST_USER_TOP_N: int = Field(default=10)  # max articles per user email
    DIGEST_USER_WINDOW_HOURS: int = Field(default=48)  # recency window per user
    DIGEST_USER_SCORE_FLOOR: float = Field(default=0.0)  # min cosine (>=0 => positively correlated)
