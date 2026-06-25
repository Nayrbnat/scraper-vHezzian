"""Per-module embedder config (never core Settings — Invariant #16).

Default provider is Google Gemini ``gemini-embedding-001`` via a free Google AI Studio
key, with output dimension pinned to 1536 to match the existing ``Vector(1536)`` columns
(no migration). Switch to an OpenAI-wire provider (e.g. Jina) via ``EMBED_PROVIDER``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbedderSettings(BaseSettings):
    """Embedding config. Overridable via environment / ``.env``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    EMBED_PROVIDER: str = Field(default="gemini")  # "gemini" | "openai_compatible"
    EMBED_API_KEY: str = Field(default="")  # secret; .env only; empty => jobs idle
    EMBED_API_BASE_URL: str = Field(default="https://generativelanguage.googleapis.com/v1beta")
    EMBED_MODEL: str = Field(default="gemini-embedding-001")
    EMBED_DIM: int = Field(default=1536)  # MUST match the Vector(N) columns
    EMBED_BATCH_SIZE: int = Field(default=100)
    EMBED_REQUEST_TIMEOUT: float = Field(default=60.0)
    EMBED_MAX_RETRIES: int = Field(default=2)
    # score_users knobs:
    EMBED_SCORE_WINDOW_DAYS: int = Field(default=30)  # only score articles fetched within window
    EMBED_TOP_K: int = Field(default=200)  # rows written per user
