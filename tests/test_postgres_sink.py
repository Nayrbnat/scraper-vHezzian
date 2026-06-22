"""Tests for ``PostgresSink`` (W4) — SPEC.md §3.18.

All tests are marked ``@pytest.mark.db`` and require a live pgvector Postgres
instance (see conftest.py for how ``DATABASE_URL`` is resolved).

Run with::

    DATABASE_URL='postgresql+asyncpg://scrapeforge:scrapeforge@localhost:55432/scrapeforge_test' \
        .venv/Scripts/python -m pytest -q -m db tests/test_postgres_sink.py
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article as ArticleDTO
from scrapeforge.core.models import ScrapeResult
from scrapeforge.core.storage.base import ArticleSink, url_id
from scrapeforge.core.storage.postgres import PostgresSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_URL = "https://example.com/article/1"
_TEST_URL_2 = "https://example.com/article/2"


def _make_article(url: str = _TEST_URL, **meta_overrides: object) -> ArticleDTO:
    """Return a minimal ArticleDTO for use in test ScrapeResults."""
    meta = {
        "source_domain": "example.com",
        "bucket": "public",
        "raw_key": "s3://bucket/raw/abc123",
        **meta_overrides,
    }
    return ArticleDTO(
        url=url,
        title="Test Article Headline",
        content="This is the full article body text, long enough to be realistic.",
        author="Test Author",
        publish_date=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
        metadata=meta,
    )


def _make_result(
    article: ArticleDTO | None = None,
    status: str = "success",
    driver_used: str = "curl_cffi",
    proxy_used: str | None = "http://proxy:8080",
    challenge_solved: bool = False,
    fetch_duration_ms: int = 350,
) -> ScrapeResult:
    """Return a ScrapeResult wrapping *article* (creates one if not given)."""
    if article is None and status == "success":
        article = _make_article()
    return ScrapeResult(
        status=status,
        driver_used=driver_used,
        article=article,
        proxy_used=proxy_used,
        challenge_solved=challenge_solved,
        fetch_duration_ms=fetch_duration_ms,
    )


# ---------------------------------------------------------------------------
# Fixture: sink bound to the same DB the db_session fixture uses
# ---------------------------------------------------------------------------


@pytest.fixture
def sink(_db_url: str) -> PostgresSink:
    """A ``PostgresSink`` sharing the same DB URL as ``db_session``.

    We derive the sessionmaker from the same ``_db_url`` session-scoped fixture
    that conftest uses, so writes from the sink and queries from db_session hit
    the same database (and after truncation, start clean for each test).
    """
    engine = create_async_engine(_db_url, echo=False)
    factory = make_sessionmaker(engine)
    return PostgresSink(factory)


# ---------------------------------------------------------------------------
# Contract-parity tests (no DB needed — use a stub session_factory)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_sink() -> PostgresSink:
    """A ``PostgresSink`` built with a dummy session_factory.

    Suitable only for contract-level tests that never call ``write()``
    (so the factory is never actually invoked).  No DB connection required.
    """
    # We pass None; the sink only uses the factory inside write(), which these
    # tests don't call.  Type is intentionally loose here for the stub.
    return PostgresSink(None)  # type: ignore[arg-type]


def test_postgres_sink_is_article_sink(stub_sink: PostgresSink) -> None:
    """``PostgresSink`` must satisfy the ``ArticleSink`` ABC."""
    assert isinstance(stub_sink, ArticleSink)


def test_seen_before_any_write_returns_false(stub_sink: PostgresSink) -> None:
    """In-process cache is empty on construction — seen() returns False."""
    assert stub_sink.seen(_TEST_URL) is False


def test_seen_is_sync(stub_sink: PostgresSink) -> None:
    """seen() must be a plain synchronous method (not a coroutine)."""
    import inspect

    result = stub_sink.seen(_TEST_URL)
    # If it were async, result would be a coroutine object, not a bool.
    assert not inspect.iscoroutine(result)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# DB tests
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_write_success_persists_row(sink: PostgresSink, db_session: AsyncSession) -> None:
    """A success ScrapeResult is persisted; querying by PK returns the row with correct fields."""
    result = _make_result()
    article = result.article
    assert article is not None

    await sink.write(result)

    expected_id = url_id(article.url)
    row = await db_session.get(ArticleRow, expected_id)

    assert row is not None, "Expected a row in articles but found none"
    assert row.id == expected_id
    assert row.url == article.url
    assert row.title == article.title
    assert row.content == article.content
    assert row.author == article.author
    assert row.domain == "example.com"
    assert row.bucket == "public"
    assert row.raw_key == "s3://bucket/raw/abc123"
    # fetched_at should be a tz-aware datetime
    assert row.fetched_at is not None
    assert row.fetched_at.tzinfo is not None


@pytest.mark.db
async def test_write_persists_provenance_in_meta(
    sink: PostgresSink, db_session: AsyncSession
) -> None:
    """The meta JSONB column carries both article metadata and scrape provenance."""
    result = _make_result(
        driver_used="patchright", proxy_used="socks5://p:8181", fetch_duration_ms=999
    )
    await sink.write(result)

    article = result.article
    assert article is not None
    row = await db_session.get(ArticleRow, url_id(article.url))
    assert row is not None

    # Scrape provenance must appear in meta.
    assert row.meta.get("driver_used") == "patchright"
    assert row.meta.get("proxy_used") == "socks5://p:8181"
    assert row.meta.get("fetch_duration_ms") == 999


@pytest.mark.db
async def test_write_persists_raw_key(sink: PostgresSink, db_session: AsyncSession) -> None:
    """raw_key from article.metadata is stored on the row."""
    article = _make_article(raw_key="s3://bucket/raw/mykey")
    result = _make_result(article=article)
    await sink.write(result)

    row = await db_session.get(ArticleRow, url_id(article.url))
    assert row is not None
    assert row.raw_key == "s3://bucket/raw/mykey"


@pytest.mark.db
async def test_write_raw_key_none_when_absent(sink: PostgresSink, db_session: AsyncSession) -> None:
    """raw_key is None when not present in metadata."""
    meta = {"source_domain": "example.com", "bucket": "public"}
    article = ArticleDTO(
        url=_TEST_URL_2,
        title="Another Article",
        content="Body text here.",
        metadata=meta,
    )
    result = _make_result(article=article)
    await sink.write(result)

    row = await db_session.get(ArticleRow, url_id(article.url))
    assert row is not None
    assert row.raw_key is None


@pytest.mark.db
async def test_write_idempotent_no_integrity_error(
    sink: PostgresSink, db_session: AsyncSession
) -> None:
    """Writing the same URL twice must NOT raise IntegrityError; exactly one row exists."""
    result = _make_result()
    await sink.write(result)
    await sink.write(result)  # second write — must be a no-op UPSERT, not an error

    article = result.article
    assert article is not None

    from sqlalchemy import func, select

    count_stmt = select(func.count()).where(ArticleRow.id == url_id(article.url))
    count_result = await db_session.execute(count_stmt)
    row_count = count_result.scalar_one()
    assert row_count == 1, f"Expected 1 row, got {row_count}"


@pytest.mark.db
async def test_write_upsert_updates_content(sink: PostgresSink, db_session: AsyncSession) -> None:
    """A second write with changed content updates the existing row (on_conflict_do_update)."""
    article_v1 = _make_article()
    result_v1 = _make_result(article=article_v1)
    await sink.write(result_v1)

    # Same URL, different title and content.
    article_v2 = ArticleDTO(
        url=_TEST_URL,
        title="Updated Headline",
        content="Updated body content after correction.",
        metadata={"source_domain": "example.com", "bucket": "public"},
    )
    result_v2 = _make_result(article=article_v2)
    await sink.write(result_v2)

    row = await db_session.get(ArticleRow, url_id(_TEST_URL))
    # Refresh to pick up changes committed by the sink.
    await db_session.refresh(row)
    assert row is not None
    assert row.title == "Updated Headline"
    assert row.content == "Updated body content after correction."


@pytest.mark.db
async def test_write_skips_non_success_status(sink: PostgresSink, db_session: AsyncSession) -> None:
    """Non-success statuses (challenge, error, …) produce no DB row."""
    for bad_status in ("challenge", "rate_limited", "error", "proxy_failed"):
        article = _make_article(url=f"https://example.com/{bad_status}")
        result = ScrapeResult(
            status=bad_status,
            driver_used="curl_cffi",
            article=article,
        )
        await sink.write(result)

    from sqlalchemy import select

    stmt = select(ArticleRow)
    rows_result = await db_session.execute(stmt)
    rows = rows_result.scalars().all()
    assert len(rows) == 0, f"Expected no rows for non-success statuses; got {len(rows)}"


@pytest.mark.db
async def test_write_skips_when_article_is_none(
    sink: PostgresSink, db_session: AsyncSession
) -> None:
    """A 'success' result with article=None is skipped — no row written."""
    result = ScrapeResult(status="success", driver_used="curl_cffi", article=None)
    await sink.write(result)

    from sqlalchemy import select

    stmt = select(ArticleRow)
    rows_result = await db_session.execute(stmt)
    rows = rows_result.scalars().all()
    assert len(rows) == 0


@pytest.mark.db
async def test_seen_returns_true_after_write(sink: PostgresSink, db_session: AsyncSession) -> None:
    """In-process cache: seen() returns True after a successful write."""
    assert sink.seen(_TEST_URL) is False
    await sink.write(_make_result())
    assert sink.seen(_TEST_URL) is True


@pytest.mark.db
async def test_seen_cache_populated_only_for_written_urls(
    sink: PostgresSink, db_session: AsyncSession
) -> None:
    """seen() returns False for URLs that were never written."""
    await sink.write(_make_result())
    assert sink.seen(_TEST_URL) is True
    assert sink.seen("https://never-written.example.com/") is False


@pytest.mark.db
async def test_close_is_no_op_and_idempotent(sink: PostgresSink) -> None:
    """close() returns None and can be called multiple times without error."""
    result = await sink.close()
    assert result is None
    result2 = await sink.close()
    assert result2 is None


@pytest.mark.db
async def test_domain_falls_back_to_url_parse_when_no_source_domain(
    sink: PostgresSink, db_session: AsyncSession
) -> None:
    """domain is parsed from the URL hostname when source_domain is absent in metadata."""
    article = ArticleDTO(
        url="https://ft.com/content/some-article",
        title="FT article",
        content="Full article body text.",
        metadata={"bucket": "premium"},  # no source_domain key
    )
    result = _make_result(article=article)
    await sink.write(result)

    row = await db_session.get(ArticleRow, url_id(article.url))
    assert row is not None
    assert row.domain == "ft.com"


@pytest.mark.db
async def test_write_and_close_async_methods(sink: PostgresSink) -> None:
    """write() and close() must be coroutine functions (async def)."""
    import inspect

    assert inspect.iscoroutinefunction(sink.write)
    assert inspect.iscoroutinefunction(sink.close)
