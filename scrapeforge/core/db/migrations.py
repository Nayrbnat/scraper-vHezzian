"""Schemaless-friendly column adds for environments without Alembic.

``@db`` tests get the schema from ``Base.metadata.create_all`` (conftest). An existing
production Postgres gets the Phase-2 columns via these idempotent ``ADD COLUMN IF NOT
EXISTS`` statements, called once at the summarizer entry point.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_STATEMENTS = (
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS relevance INTEGER",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS summary JSONB",
    "CREATE INDEX IF NOT EXISTS ix_articles_relevance ON articles (relevance)",
)


async def ensure_summary_columns(engine: AsyncEngine) -> None:
    """Idempotently add the relevance/summary columns + relevance index to ``articles``."""
    async with engine.begin() as conn:
        for stmt in _STATEMENTS:
            await conn.execute(text(stmt))
