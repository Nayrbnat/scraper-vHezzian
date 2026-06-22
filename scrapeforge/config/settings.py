"""Core/shared configuration for ScrapeForge (SPEC.md §8).

Only genuinely shared keys live here.  Feature-specific keys (REDDIT_*, SUBSTACK_*,
per-bucket concurrency, etc.) belong in each feature's own ``BaseSettings`` fragment
inside its module — this keeps ``config/settings.py`` off the merge-conflict hot path
(Invariant #16).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Core runtime settings loaded from environment variables / ``.env``.

    All keys here are shared across multiple features.  Per-feature settings
    fragments (e.g. ``RedditSettings``) inherit ``BaseSettings`` separately and
    read the same ``.env`` file without touching this class.
    """

    # ------------------------------------------------------------------
    # Drivers
    # ------------------------------------------------------------------
    PATCHRIGHT_CHANNEL: str = "chrome"  # MUST be 'chrome', never 'chromium'
    CURL_CFFI_IMPERSONATE: str = "chrome"  # Generic alias
    PRIMP_IMPERSONATE_OS: str = "windows"  # windows | macos | linux

    # ------------------------------------------------------------------
    # Auth / session store
    # ------------------------------------------------------------------
    STATE_STORE_KEY: str  # 32+ char Fernet-compatible key — REQUIRED, no default
    STATE_STORE_PATH: Path = Path.home() / ".scrapeforge" / "states"
    SESSION_TTL_DAYS: int = 7

    # ------------------------------------------------------------------
    # Proxies
    # ------------------------------------------------------------------
    PROXY_LIST_PATH: Path = Path("./proxies.txt")
    PROXY_ROTATION_POLICY: Literal["per_context", "per_request", "off"] = "per_context"
    PROXY_HEALTH_CHECK_URL: str = "https://httpbin.org/ip"
    PROXY_BURNED_COOLDOWN_MINUTES: int = 60

    # ------------------------------------------------------------------
    # Humanization
    # ------------------------------------------------------------------
    HUMANIZE_MIN_DELAY: float = 2.0
    HUMANIZE_MAX_DELAY: float = 8.0
    HUMANIZE_MOUSE_SPEED_MEAN: float = 500.0  # pixels/sec
    HUMANIZE_TYPING_MEAN_MS: float = 80.0
    HUMANIZE_TYPING_STD_MS: float = 20.0

    # ------------------------------------------------------------------
    # Circuit breaker (reactive per-domain failure policy)
    # ------------------------------------------------------------------
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_PAUSE_MINUTES: int = 30

    # ------------------------------------------------------------------
    # Rate limiting (proactive per-domain politeness)
    # ------------------------------------------------------------------
    DEFAULT_RATE_INTERVAL_SECONDS: float = 1.0
    PREMIUM_MIN_INTERVAL_SECONDS: float = 60.0  # never batch premium

    # ------------------------------------------------------------------
    # Parsing / extraction
    # ------------------------------------------------------------------
    HTML_PARSER: Literal["selectolax", "lxml"] = "selectolax"
    MIN_CONTENT_LENGTH: int = 500  # soft-block floor for response_is_valid()

    # ------------------------------------------------------------------
    # Storage / output
    # ------------------------------------------------------------------
    OUTPUT_FORMAT: Literal["jsonl"] = "jsonl"  # RAG-ready sink
    DEFAULT_OUTPUT_PATH: Path = Path("./output")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["text", "json"] = "text"

    # ------------------------------------------------------------------
    # Serving plane / ingestion pipeline (Phase 6) — core/shared infra
    # ------------------------------------------------------------------
    # Datastore (async DSN, e.g. postgresql+asyncpg://user:pass@host:5432/db)
    DATABASE_URL: str = "postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5432/scrapeforge"
    # Redis (queue backend; SQS/Cloud Tasks adapters slot in later behind MessageQueue)
    REDIS_URL: str = "redis://localhost:6379/0"
    JOB_QUEUE: str = "scrapeforge:jobs"  # API -> scraper workers
    RESULTS_QUEUE: str = "scrapeforge:results"  # scraper -> transform workers
    DLQ_SUFFIX: str = ":dlq"  # dead-letter stream suffix (poison messages)
    QUEUE_MAX_RETRIES: int = 5  # attempts before a message is dead-lettered

    # Object store (raw payloads; MinIO now, real S3/GCS by endpoint config)
    OBJECT_STORE_ENDPOINT: str = "http://localhost:9000"  # MinIO; empty/None => AWS default
    OBJECT_STORE_BUCKET: str = "scrapeforge-raw"
    # Defaults are MinIO's well-known LOCAL dev credentials; production overrides via env.
    OBJECT_STORE_ACCESS_KEY: str = "minioadmin"
    OBJECT_STORE_SECRET_KEY: str = "minioadmin"  # noqa: S105  (local MinIO default, not a secret)
    OBJECT_STORE_REGION: str = "us-east-1"

    # API auth (comma-separated keys -> set; per-key rate limit)
    API_KEYS: str = ""  # e.g. "key1,key2"; parse via api_key_set
    API_RATE_LIMIT_PER_MIN: int = 120

    # ------------------------------------------------------------------
    # Pydantic-settings config
    # ------------------------------------------------------------------
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # unknown env vars are silently ignored
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("STATE_STORE_KEY")
    @classmethod
    def _key_must_be_32_chars(cls, v: str) -> str:
        """Defense-in-depth guard: enforce a minimum length of 32 characters.

        This does NOT validate full Fernet key format — that is the caller's
        responsibility.  The check simply rejects obviously short values that
        could never be a valid key.
        """
        if len(v) < 32:
            raise ValueError(
                f"STATE_STORE_KEY must be at least 32 characters (got {len(v)}). "
                'Generate one with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        return v

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    def api_key_set(self) -> set[str]:
        """Parse ``API_KEYS`` (comma-separated) into a set of valid keys.

        Empty/whitespace entries are dropped.  An empty result means no key is
        configured — the API auth layer treats that as "reject all" (fail closed).
        """
        return {k.strip() for k in self.API_KEYS.split(",") if k.strip()}
