"""Tests for core/db/ — models, session, and repositories (W3 datastore).

All tests are marked ``@pytest.mark.db`` so they are collected but only run where
a Postgres instance is reachable (controlled by ``DATABASE_URL`` env var).  CI
sets ``DATABASE_URL`` to the pgvector service container; locally, set it to a
running pgvector container.  If the DB is unreachable the ``_db_engine`` fixture
in ``tests/conftest.py`` skips the entire group with a clear message.

TDD order:
  RED  — write these tests first; they fail because the modules don't exist yet.
  GREEN — implement models / session / repositories to pass them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """UTC-aware 'now' helper."""
    return datetime.now(UTC)


def _article_kwargs(**overrides):
    """Return a minimal valid dict for constructing an Article ORM row."""
    base = {
        "id": "a" * 64,  # sha256 hex digest length
        "url": "https://example.com/article",
        "domain": "example.com",
        "bucket": "public",
        "title": "Test Article",
        "content": "Body text here.",
        "fetched_at": _now(),
        "meta": {"driver_used": "curl_cffi"},
    }
    base.update(overrides)
    return base


def _job_kwargs(**overrides):
    """Return a minimal valid dict for constructing a Job ORM row."""
    base = {
        "id": str(uuid.uuid4()),
        "status": "queued",
        "source": "example.com",
        "params": {"bucket": "public"},
        "created_at": _now(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Article round-trip
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_article_round_trip(db_session: AsyncSession) -> None:
    """Insert an Article then select it back; all fields must survive intact."""
    from scrapeforge.core.db.models import Article

    kwargs = _article_kwargs(
        author="Jane Doe",
        publish_date=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
        raw_key="raw/abc123.json",
        meta={"driver_used": "curl_cffi", "proxy_used": "10.0.0.1:8080"},
    )
    article = Article(**kwargs)
    db_session.add(article)
    await db_session.commit()

    result = await db_session.get(Article, kwargs["id"])
    assert result is not None
    assert result.url == kwargs["url"]
    assert result.domain == kwargs["domain"]
    assert result.bucket == kwargs["bucket"]
    assert result.title == kwargs["title"]
    assert result.content == kwargs["content"]
    assert result.author == "Jane Doe"
    assert result.publish_date is not None
    assert result.raw_key == "raw/abc123.json"
    assert result.meta == {"driver_used": "curl_cffi", "proxy_used": "10.0.0.1:8080"}
    assert result.embedding is None  # embeddings deferred


@pytest.mark.db
async def test_article_optional_fields_null(db_session: AsyncSession) -> None:
    """Optional fields (author, publish_date, raw_key, embedding) default to None."""
    from scrapeforge.core.db.models import Article

    article = Article(**_article_kwargs())
    db_session.add(article)
    await db_session.commit()

    result = await db_session.get(Article, "a" * 64)
    assert result is not None
    assert result.author is None
    assert result.publish_date is None
    assert result.raw_key is None
    assert result.embedding is None


# ---------------------------------------------------------------------------
# 2. Timezone-aware datetime round-trip (Fix 2)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_article_datetime_tzinfo(db_session: AsyncSession) -> None:
    """Stored tz-aware datetime must come back with tzinfo != None (TIMESTAMPTZ)."""
    from scrapeforge.core.db.models import Article

    aware_ts = datetime(2024, 6, 15, 8, 30, 0, tzinfo=UTC)
    article = Article(
        **_article_kwargs(
            id="b" * 64,
            url="https://example.com/tz-test",
            fetched_at=aware_ts,
            publish_date=aware_ts,
        )
    )
    db_session.add(article)
    await db_session.commit()

    # Expunge so we get a fresh SELECT, not the identity-map cached instance.
    db_session.expunge_all()
    result = await db_session.get(Article, "b" * 64)
    assert result is not None
    assert result.fetched_at.tzinfo is not None, "fetched_at must be tz-aware (TIMESTAMPTZ)"
    assert result.publish_date is not None
    assert result.publish_date.tzinfo is not None, "publish_date must be tz-aware (TIMESTAMPTZ)"


# ---------------------------------------------------------------------------
# 3. PK dedup (duplicate insert raises IntegrityError) — Fix 4: expunge_all()
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_article_pk_dedup_raises(db_session: AsyncSession) -> None:
    """Inserting two Articles with the same id must raise IntegrityError (PK constraint)."""
    from scrapeforge.core.db.models import Article

    a1 = Article(**_article_kwargs())
    db_session.add(a1)
    await db_session.commit()

    # Expunge all so SQLAlchemy's identity map is cleared — the next INSERT
    # will actually hit Postgres and trigger the PK constraint violation.
    db_session.expunge_all()

    a2 = Article(**_article_kwargs(url="https://example.com/other"))
    db_session.add(a2)

    with pytest.raises(IntegrityError):
        await db_session.commit()

    await db_session.rollback()


# ---------------------------------------------------------------------------
# 4. Job status transitions via update_job_status
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_job_status_queued_to_running(db_session: AsyncSession) -> None:
    """Transition queued -> running: started_at is set, status is 'running'."""
    from scrapeforge.core.db import repositories as repo

    job = await repo.create_job(
        db_session, job_id=str(uuid.uuid4()), source="example.com", params={}
    )
    assert job.status == "queued"
    assert job.started_at is None

    await repo.update_job_status(db_session, job.id, status="running", started=True)

    fetched = await repo.get_job(db_session, job.id)
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.started_at is not None


@pytest.mark.db
async def test_job_status_running_to_done(db_session: AsyncSession) -> None:
    """Transition running -> done: finished_at is set, result_count is updated."""
    from scrapeforge.core.db import repositories as repo

    job = await repo.create_job(
        db_session, job_id=str(uuid.uuid4()), source="example.com", params={}
    )
    await repo.update_job_status(db_session, job.id, status="running", started=True)
    await repo.update_job_status(db_session, job.id, status="done", result_count=42, finished=True)

    fetched = await repo.get_job(db_session, job.id)
    assert fetched is not None
    assert fetched.status == "done"
    assert fetched.finished_at is not None
    assert fetched.result_count == 42


@pytest.mark.db
async def test_job_status_error_path(db_session: AsyncSession) -> None:
    """Error path: status='error' and error message are persisted."""
    from scrapeforge.core.db import repositories as repo

    job = await repo.create_job(
        db_session, job_id=str(uuid.uuid4()), source="example.com", params={}
    )
    await repo.update_job_status(
        db_session,
        job.id,
        status="error",
        error="Connection refused",
        finished=True,
    )

    fetched = await repo.get_job(db_session, job.id)
    assert fetched is not None
    assert fetched.status == "error"
    assert fetched.error == "Connection refused"
    assert fetched.finished_at is not None


# ---------------------------------------------------------------------------
# 5. query_articles — filtering and pagination
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_query_articles_filters_and_pagination(db_session: AsyncSession) -> None:
    """Insert 3 articles; filter by domain/bucket; verify limit/offset pagination."""
    from scrapeforge.core.db import repositories as repo
    from scrapeforge.core.db.models import Article

    articles = [
        Article(
            **_article_kwargs(
                id="a" * 63 + "1",
                url="https://example.com/a1",
                domain="example.com",
                bucket="public",
            )
        ),
        Article(
            **_article_kwargs(
                id="a" * 63 + "2",
                url="https://other.com/a2",
                domain="other.com",
                bucket="public",
            )
        ),
        Article(
            **_article_kwargs(
                id="a" * 63 + "3",
                url="https://example.com/a3",
                domain="example.com",
                bucket="premium",
            )
        ),
    ]
    for a in articles:
        db_session.add(a)
    await db_session.commit()

    # Filter by domain=example.com — should return a1 and a3
    results = await repo.query_articles(db_session, domain="example.com")
    assert len(results) == 2
    result_domains = {r.domain for r in results}
    assert result_domains == {"example.com"}

    # Filter by bucket=premium — should return only a3
    results = await repo.query_articles(db_session, bucket="premium")
    assert len(results) == 1
    assert results[0].bucket == "premium"

    # No filter, limit=2
    results = await repo.query_articles(db_session, limit=2)
    assert len(results) == 2

    # No filter, limit=2, offset=2 — should return 1 article
    results = await repo.query_articles(db_session, limit=2, offset=2)
    assert len(results) == 1

    # Combined domain + bucket filter — should return 0
    results = await repo.query_articles(db_session, domain="other.com", bucket="premium")
    assert len(results) == 0


# ---------------------------------------------------------------------------
# 6. create_job / get_job / list_jobs round-trip
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_create_and_get_job(db_session: AsyncSession) -> None:
    """create_job sets status='queued' and created_at; get_job retrieves it."""
    from scrapeforge.core.db import repositories as repo

    jid = str(uuid.uuid4())
    job = await repo.create_job(
        db_session,
        job_id=jid,
        source="ft.com",
        params={"bucket": "premium", "limit": 10},
    )

    assert job.id == jid
    assert job.status == "queued"
    assert job.source == "ft.com"
    assert job.params == {"bucket": "premium", "limit": 10}
    assert job.created_at is not None
    assert job.started_at is None
    assert job.finished_at is None
    assert job.result_count == 0

    fetched = await repo.get_job(db_session, jid)
    assert fetched is not None
    assert fetched.id == jid


@pytest.mark.db
async def test_get_job_missing_returns_none(db_session: AsyncSession) -> None:
    """get_job for a non-existent id returns None."""
    from scrapeforge.core.db import repositories as repo

    result = await repo.get_job(db_session, "does-not-exist")
    assert result is None


@pytest.mark.db
async def test_list_jobs_most_recent_first(db_session: AsyncSession) -> None:
    """list_jobs returns jobs ordered most-recent-first (created_at desc)."""
    from datetime import timedelta

    from scrapeforge.core.db import repositories as repo
    from scrapeforge.core.db.models import Job

    now = _now()
    ids = [str(uuid.uuid4()) for _ in range(3)]

    # Insert with explicit created_at offsets so ordering is unambiguous.
    for i, job_id in enumerate(ids):
        job = Job(
            id=job_id,
            status="queued",
            source="example.com",
            params={},
            created_at=now - timedelta(hours=i),
        )
        db_session.add(job)
    await db_session.commit()

    jobs = await repo.list_jobs(db_session)
    # Most recent (ids[0]) should be first
    assert jobs[0].id == ids[0]
    assert jobs[-1].id == ids[-1]


# ---------------------------------------------------------------------------
# 7. Session / engine creation (no live DB needed)
# ---------------------------------------------------------------------------


def test_make_engine_and_sessionmaker_are_importable() -> None:
    """make_engine and make_sessionmaker must be importable and callable."""
    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    # Just check they are callable (no I/O at import time).
    assert callable(make_engine)
    assert callable(make_sessionmaker)


async def test_make_engine_accepts_explicit_url() -> None:
    """make_engine accepts an explicit DSN without reading Settings."""
    from scrapeforge.core.db.session import make_engine

    engine = make_engine("postgresql+asyncpg://user:pass@localhost:5432/testdb")
    # Should be an AsyncEngine; dispose is safe to call immediately (no connection opened yet).
    await engine.dispose()


def test_make_sessionmaker_wraps_engine() -> None:
    """make_sessionmaker returns an async_sessionmaker bound to the engine."""
    from sqlalchemy.ext.asyncio import async_sessionmaker as _asm

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    engine = make_engine("postgresql+asyncpg://user:pass@localhost:5432/testdb")
    sm = make_sessionmaker(engine)
    assert isinstance(sm, _asm)
