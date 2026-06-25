"""Per-module config for the hezzian->scraper_news user sync (Invariant #16)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class UserSyncSettings(BaseSettings):
    """Where to read the app users from. Empty HEZZIAN_DATABASE_URL => the sync idle-skips."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    HEZZIAN_DATABASE_URL: str = Field(default="")  # read-only source DB (the hezzian app database)
