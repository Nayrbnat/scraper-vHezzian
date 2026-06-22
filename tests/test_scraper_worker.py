"""Tests for scrapeforge.worker.scraper_worker (W5 — stateless scraper stage).

All tests are DB-free and network-free.  Infrastructure is provided by the
in-memory fakes: ``InMemoryMessageQueue`` and ``InMemoryObjectStore``.
"""

from __future__ import annotations

import json
import types

import pytest

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.objectstore.memory import InMemoryObjectStore
from scrapeforge.core.queue.memory import InMemoryMessageQueue
from scrapeforge.core.storage.base import url_id
from scrapeforge.worker.messages import JobMessage, raw_object_key

# ---------------------------------------------------------------------------
# Fake engine
# ---------------------------------------------------------------------------


class _FakeEngine:
    """Minimal scrape engine that returns a canned ScrapeResult without network I/O."""

    def __init__(self, result: ScrapeResult) -> None:
        self._result = result

    async def scrape(self, url: str) -> ScrapeResult:  # noqa: ARG002
        return self._result


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_settings():
    """Tiny settings-like object; avoids env-var requirements."""
    return types.SimpleNamespace(
        JOB_QUEUE="jobs",
        RESULTS_QUEUE="results",
        QUEUE_MAX_RETRIES=2,
    )


@pytest.fixture
def success_engine():
    article = Article(
        url="https://x.com/article/1",
        title="Test Article",
        content="Content here.",
        raw_html="<html><body>Test Article Content here.</body></html>",
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    return _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))


@pytest.fixture
def challenge_engine():
    return _FakeEngine(
        ScrapeResult(
            status="challenge",
            driver_used="curl_cffi",
            article=None,
            error="blocked",
        )
    )


TEST_URL = "https://x.com/article/1"


# ---------------------------------------------------------------------------
# Helper to build a JobMessage
# ---------------------------------------------------------------------------


def _job(url: str = TEST_URL, bucket: str | None = None) -> JobMessage:
    return JobMessage(job_id="job-abc-123", url=url, bucket=bucket)


# ---------------------------------------------------------------------------
# 1. Success path
# ---------------------------------------------------------------------------


async def test_success_path_stores_raw_html() -> None:
    """Raw HTML bytes must be stored at the deterministic object-store key."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    article = Article(
        url=TEST_URL,
        title="Test",
        content="Content.",
        raw_html="<html><body>Test Article Content here.</body></html>",
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    engine = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    await handle_scrape_job(
        _job(),
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    expected_key = raw_object_key("public", url_id(TEST_URL))
    assert await store.exists(expected_key), "raw HTML not stored at expected key"
    stored_bytes = await store.get(expected_key)
    assert stored_bytes == b"<html><body>Test Article Content here.</body></html>"


async def test_success_path_publishes_pointer() -> None:
    """A ResultPointer with correct fields must be published to results_queue."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    article = Article(
        url=TEST_URL,
        title="Test",
        content="Content.",
        raw_html="<html><body>Test Article Content here.</body></html>",
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    engine = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    pointer = await handle_scrape_job(
        _job(),
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    # Returned pointer has correct fields.
    assert pointer["url"] == TEST_URL
    assert pointer["url_id"] == url_id(TEST_URL)
    assert pointer["domain"] == "x.com"
    assert pointer["bucket"] == "public"
    assert pointer["status"] == "success"
    assert pointer["object_key"] == raw_object_key("public", url_id(TEST_URL))
    assert pointer["job_id"] == "job-abc-123"
    assert "fetched_at" in pointer

    # Also published to queue.
    assert await queue.size("results") == 1
    msg = await queue.reserve("results")
    assert msg is not None
    published = msg.payload
    assert published["url_id"] == url_id(TEST_URL)
    assert published["domain"] == "x.com"
    assert published["status"] == "success"


async def test_fetched_at_is_utc_iso8601() -> None:
    """pointer['fetched_at'] must be a valid, timezone-aware ISO-8601 UTC string."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    article = Article(
        url=TEST_URL,
        title="Test",
        content="Content.",
        raw_html="<html>ts</html>",
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    engine = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    pointer = await handle_scrape_job(
        _job(),
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    from datetime import UTC, datetime

    ts = datetime.fromisoformat(pointer["fetched_at"])  # must not raise
    assert ts.tzinfo is not None, "fetched_at must be timezone-aware"
    assert ts.tzinfo == UTC or ts.utcoffset().total_seconds() == 0, "fetched_at must be UTC"


async def test_success_path_is_db_free() -> None:
    """The worker module must not import any DB-related module at the source level.

    Asserts the DB-free invariant via AST inspection of the module source so that
    aliased imports (``from x import y as z``), function-local imports, and
    ``from scrapeforge.core.db import session`` are all caught — not just module
    attribute presence.
    """
    import ast
    import importlib
    from pathlib import Path

    import scrapeforge.worker.scraper_worker as worker_mod

    # Re-resolve the source path from the imported module (handles editable installs).
    source_path = importlib.util.find_spec(worker_mod.__name__).origin
    source = Path(source_path).read_text(encoding="utf-8")  # noqa: ASYNC240
    tree = ast.parse(source)

    forbidden_fragments = {
        "scrapeforge.core.db",
        "postgres",
        "PostgresSink",
        "sqlalchemy",
        "session",
        "sessionmaker",
    }

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_path = alias.name
                for frag in forbidden_fragments:
                    if frag in module_path:
                        violations.append(
                            f"Import {module_path!r} contains forbidden fragment {frag!r}"
                        )
        elif isinstance(node, ast.ImportFrom):
            module_path = node.module or ""
            # Check the module path itself.
            for frag in forbidden_fragments:
                if frag in module_path:
                    violations.append(
                        f"from-import module {module_path!r} contains forbidden fragment {frag!r}"
                    )
            # Also check each imported name (catches ``from x import PostgresSink``).
            for alias in node.names:
                for frag in forbidden_fragments:
                    if frag in alias.name:
                        violations.append(
                            f"from {module_path!r} import {alias.name!r} "
                            f"contains forbidden fragment {frag!r}"
                        )

    assert not violations, (
        "scraper_worker.py imports DB-related symbols (stateless-scraper invariant violated):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# 2. Challenge path
# ---------------------------------------------------------------------------


async def test_challenge_path_stores_json_fallback() -> None:
    """When there is no article/html, a JSON-encoded fallback must be archived."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    engine = _FakeEngine(
        ScrapeResult(status="challenge", driver_used="curl_cffi", article=None, error="blocked")
    )

    pointer = await handle_scrape_job(
        _job(),
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    expected_key = raw_object_key("public", url_id(TEST_URL))
    assert await store.exists(expected_key)

    stored_bytes = await store.get(expected_key)
    payload = json.loads(stored_bytes.decode("utf-8"))
    assert payload["status"] == "challenge"
    assert payload["error"] == "blocked"
    assert payload["url"] == TEST_URL

    # Pointer has challenge status.
    assert pointer["status"] == "challenge"


async def test_article_present_but_raw_html_none_uses_json_fallback() -> None:
    """Article present with raw_html=None must still archive a JSON fallback (not crash).

    The ``if result.article is not None and result.article.raw_html`` branch covers
    the falsy-html case: an Article with raw_html=None should fall through to the JSON
    fallback path, not attempt ``.encode()`` on None.
    """
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    # Article with raw_html explicitly None.
    article_no_html = Article(
        url=TEST_URL,
        title="No HTML",
        content="Parsed text only.",
        raw_html=None,
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    engine = _FakeEngine(
        ScrapeResult(status="success", driver_used="curl_cffi", article=article_no_html)
    )

    pointer = await handle_scrape_job(
        _job(),
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    expected_key = raw_object_key("public", url_id(TEST_URL))
    assert await store.exists(expected_key)

    stored_bytes = await store.get(expected_key)
    # Must be JSON fallback, not html bytes.
    fallback = json.loads(stored_bytes.decode("utf-8"))
    assert fallback["url"] == TEST_URL
    assert fallback["status"] == "success"  # status from the ScrapeResult, not overridden

    # Verify content-type stored by inspecting the internal store dict directly.
    _, stored_ct = store._objects[expected_key]
    assert stored_ct == "application/json"

    assert pointer["status"] == "success"


async def test_article_present_but_raw_html_empty_string_uses_json_fallback() -> None:
    """Article with raw_html='' (empty string, also falsy) must use the JSON fallback path."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    article_empty_html = Article(
        url=TEST_URL,
        title="Empty HTML",
        content="Parsed text only.",
        raw_html="",
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    engine = _FakeEngine(
        ScrapeResult(status="success", driver_used="curl_cffi", article=article_empty_html)
    )

    pointer = await handle_scrape_job(
        _job(),
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    stored_bytes = await store.get(raw_object_key("public", url_id(TEST_URL)))
    fallback = json.loads(stored_bytes.decode("utf-8"))
    assert fallback["url"] == TEST_URL

    _, stored_ct = store._objects[raw_object_key("public", url_id(TEST_URL))]
    assert stored_ct == "application/json"

    assert pointer["status"] == "success"


# ---------------------------------------------------------------------------
# 3. Deterministic key / idempotent PUT
# ---------------------------------------------------------------------------


async def test_same_url_overwrites_same_key() -> None:
    """Calling handle_scrape_job twice for the same URL writes the SAME key (overwrite)."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    article = Article(
        url=TEST_URL,
        title="Test",
        content="Content.",
        raw_html="<html>first</html>",
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    engine = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    await handle_scrape_job(
        _job(), engine=engine, store=store, queue=queue, results_queue="results"
    )

    # Second call with updated HTML.
    article2 = Article(
        url=TEST_URL,
        title="Test",
        content="Content.",
        raw_html="<html>second</html>",
        metadata={"bucket": "public", "source_domain": "x.com"},
    )
    engine2 = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article2))
    await handle_scrape_job(
        _job(), engine=engine2, store=store, queue=queue, results_queue="results"
    )

    # Only ONE key should exist — it was overwritten, not duplicated.
    expected_key = raw_object_key("public", url_id(TEST_URL))
    stored = await store.get(expected_key)
    assert stored == b"<html>second</html>"

    # But two pointers published (one per call).
    assert await queue.size("results") == 2


# ---------------------------------------------------------------------------
# 4. run_scraper_worker drains the job queue
# ---------------------------------------------------------------------------


async def test_run_scraper_worker_drains_queue(fake_settings) -> None:
    """run_scraper_worker must process all waiting jobs and leave JOB_QUEUE empty."""
    from scrapeforge.worker.scraper_worker import run_scraper_worker

    queue = InMemoryMessageQueue()
    store = InMemoryObjectStore()
    article = Article(
        url="https://example.com/1",
        title="A",
        content="C.",
        raw_html="<html>1</html>",
        metadata={"bucket": "community", "source_domain": "example.com"},
    )
    engine = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    # Publish two jobs.
    url1 = "https://example.com/1"
    url2 = "https://example.com/2"
    await queue.publish(
        fake_settings.JOB_QUEUE,
        JobMessage(job_id="j1", url=url1, bucket=None),
    )
    await queue.publish(
        fake_settings.JOB_QUEUE,
        JobMessage(job_id="j2", url=url2, bucket=None),
    )

    await run_scraper_worker(
        queue=queue,
        store=store,
        engine=engine,
        settings=fake_settings,
    )

    # Job queue must be empty after drain.
    assert await queue.size(fake_settings.JOB_QUEUE) == 0

    # Two pointers on the results queue.
    assert await queue.size(fake_settings.RESULTS_QUEUE) == 2


# ---------------------------------------------------------------------------
# 5. bucket fallback: payload bucket → article metadata → "public"
# ---------------------------------------------------------------------------


async def test_bucket_falls_back_to_public_when_absent() -> None:
    """When no bucket is provided and article has no metadata bucket, default to 'public'."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    # Article with no bucket in metadata.
    article = Article(
        url=TEST_URL,
        title="Test",
        content="Content.",
        raw_html="<html>hello</html>",
        metadata={"source_domain": "x.com"},  # no 'bucket' key
    )
    engine = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    pointer = await handle_scrape_job(
        _job(bucket=None),
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    assert pointer["bucket"] == "public"
    assert pointer["object_key"].startswith("raw/public/")


async def test_bucket_from_job_message_takes_precedence() -> None:
    """When bucket is provided in the job message, it overrides article metadata."""
    from scrapeforge.worker.scraper_worker import handle_scrape_job

    store = InMemoryObjectStore()
    queue = InMemoryMessageQueue()
    article = Article(
        url=TEST_URL,
        title="Test",
        content="Content.",
        raw_html="<html>hello</html>",
        metadata={"bucket": "public", "source_domain": "x.com"},  # metadata says public
    )
    engine = _FakeEngine(ScrapeResult(status="success", driver_used="curl_cffi", article=article))

    pointer = await handle_scrape_job(
        _job(bucket="premium"),  # job says premium — takes precedence
        engine=engine,
        store=store,
        queue=queue,
        results_queue="results",
    )

    assert pointer["bucket"] == "premium"
    assert pointer["object_key"].startswith("raw/premium/")
