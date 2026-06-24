"""Per-module configuration for the summarizer (SPEC.md Invariant #16 — never core Settings)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SummarizerSettings(BaseSettings):
    """LLM summarizer config. Overridable via environment / ``.env``.

    The default provider is the free Zhipu GLM-4.5-Flash on its OpenAI-compatible base
    URL; switch providers (DeepSeek/Qwen) by changing the three SUMMARY_API_* values.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SUMMARY_API_BASE_URL: str = Field(default="https://api.z.ai/api/paas/v4")
    SUMMARY_API_KEY: str = Field(default="")  # secret; .env only; empty => worker idles
    SUMMARY_MODEL: str = Field(default="glm-4.5-flash")
    SUMMARY_PORTFOLIO: str = Field(default="")  # CSV → criterion #1
    SUMMARY_INTERESTS: str = Field(default="")  # CSV → criterion #4
    # The topical focus the relevance score prioritizes (and the briefing is framed around).
    SUMMARY_FOCUS: str = Field(default="artificial intelligence and finance")
    SUMMARY_BATCH_SIZE: int = Field(default=20)
    SUMMARY_MAX_INPUT_CHARS: int = Field(default=12000)
    SUMMARY_REQUEST_TIMEOUT: float = Field(default=90.0)  # GLM-4.5 reasoning calls run 12-30s+
    SUMMARY_INTER_REQUEST_DELAY: float = Field(default=1.0)
    SUMMARY_MAX_RETRIES: int = Field(default=2)  # 429/timeout retries before LLMRateLimitError

    def portfolio(self) -> list[str]:
        """Parse SUMMARY_PORTFOLIO (CSV) → clean list."""
        return [p.strip() for p in self.SUMMARY_PORTFOLIO.split(",") if p.strip()]

    def interests(self) -> list[str]:
        """Parse SUMMARY_INTERESTS (CSV) → clean list."""
        return [i.strip() for i in self.SUMMARY_INTERESTS.split(",") if i.strip()]
