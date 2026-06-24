"""@db: init_db is idempotent and produces the articles schema (+ relevance/summary cols)."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.db
async def test_init_db_idempotent_and_creates_schema(_db_url) -> None:
    from scrapeforge.pipeline.jobs import init_db

    engine = create_async_engine(_db_url, echo=False)
    try:
        await init_db(engine)
        await init_db(engine)  # idempotent — must not raise

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            cols = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("articles")}
            )
            ext = (
                await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))
            ).first()
        assert "articles" in tables
        assert {"relevance", "summary"} <= cols
        assert ext is not None  # pgvector enabled
    finally:
        await engine.dispose()


def test_pipeline_subapp_mounted() -> None:
    from typer.testing import CliRunner

    from scrapeforge.cli import app

    result = CliRunner().invoke(app, ["pipeline", "--help"])
    assert result.exit_code == 0
    assert "init-db" in result.stdout
