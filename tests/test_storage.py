"""Tests for scrapeforge.core.storage (SPEC.md §3.18).

TDD: tests are written before the implementation.

All tests are async (asyncio_mode=auto in pyproject.toml).
``tmp_path`` is a standard pytest fixture providing an isolated temp directory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.storage.base import ArticleSink, url_id
from scrapeforge.core.storage.jsonl import JsonlSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article(
    url: str = "https://example.com/article",
    title: str = "Test Article",
    content: str = "This is the article body content.",
) -> Article:
    return Article(url=url, title=title, content=content)


def _make_result(
    article: Article | None = None,
    status: str = "success",
    driver_used: str = "curl_cffi",
) -> ScrapeResult:
    if article is None:
        article = _make_article()
    return ScrapeResult(status=status, driver_used=driver_used, article=article)


# ---------------------------------------------------------------------------
# url_id
# ---------------------------------------------------------------------------


def test_url_id_is_sha256_hex():
    """url_id returns hex-encoded SHA-256 of the URL."""
    url = "https://example.com/test"
    expected = hashlib.sha256(url.encode()).hexdigest()
    assert url_id(url) == expected


def test_url_id_different_urls_differ():
    assert url_id("https://a.com") != url_id("https://b.com")


def test_url_id_same_url_stable():
    url = "https://stable.com/article"
    assert url_id(url) == url_id(url)


# ---------------------------------------------------------------------------
# ArticleSink is abstract
# ---------------------------------------------------------------------------


def test_article_sink_is_abstract():
    """ArticleSink cannot be instantiated directly — it is an ABC."""
    with pytest.raises(TypeError):
        ArticleSink()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# JsonlSink — basic write + seen
# ---------------------------------------------------------------------------


async def test_write_success_creates_jsonl_line(tmp_path: Path):
    """Writing a success result appends exactly one JSON line to the .jsonl file."""
    sink = JsonlSink(tmp_path / "output")
    await sink.write(_make_result())
    await sink.close()

    lines = sink.path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["url"] == "https://example.com/article"
    assert data["title"] == "Test Article"


async def test_write_success_creates_manifest_entry(tmp_path: Path):
    """A successful write appends the url_id to the manifest file."""
    url = "https://example.com/article"
    sink = JsonlSink(tmp_path / "output")
    await sink.write(_make_result(_make_article(url=url)))
    await sink.close()

    manifest_ids = sink.manifest_path.read_text(encoding="utf-8").strip().split("\n")
    assert url_id(url) in manifest_ids


async def test_seen_returns_true_after_write(tmp_path: Path):
    """seen() returns True for a URL that was written."""
    url = "https://example.com/seen-article"
    sink = JsonlSink(tmp_path / "output")
    await sink.write(_make_result(_make_article(url=url)))

    assert sink.seen(url) is True


async def test_seen_returns_false_for_unknown_url(tmp_path: Path):
    """seen() returns False for a URL that was never written."""
    sink = JsonlSink(tmp_path / "output")
    assert sink.seen("https://never-seen.com/x") is False


# ---------------------------------------------------------------------------
# JsonlSink — non-success / no-article skipping
# ---------------------------------------------------------------------------


async def test_write_skips_non_success_status(tmp_path: Path):
    """write() skips results whose status is not 'success'."""
    result = ScrapeResult(status="error", driver_used="curl_cffi", article=_make_article())
    sink = JsonlSink(tmp_path / "output")
    await sink.write(result)
    await sink.close()

    assert not sink.path.exists() or sink.path.read_text(encoding="utf-8").strip() == ""


async def test_write_skips_result_with_no_article(tmp_path: Path):
    """write() skips results where article is None, even if status is 'success'."""
    result = ScrapeResult(status="success", driver_used="curl_cffi", article=None)
    sink = JsonlSink(tmp_path / "output")
    await sink.write(result)
    await sink.close()

    assert not sink.path.exists() or sink.path.read_text(encoding="utf-8").strip() == ""


# ---------------------------------------------------------------------------
# JsonlSink — content-hash deduplication
# ---------------------------------------------------------------------------


async def test_content_dedup_skips_duplicate_content(tmp_path: Path):
    """Two articles with the same content but different URLs: second write is skipped."""
    shared_content = "Identical article body."
    article1 = _make_article(url="https://site.com/page1", content=shared_content)
    article2 = _make_article(url="https://site.com/page2", content=shared_content)

    sink = JsonlSink(tmp_path / "output")
    await sink.write(_make_result(article1))
    await sink.write(_make_result(article2))
    await sink.close()

    lines = [ln for ln in sink.path.read_text(encoding="utf-8").strip().split("\n") if ln]
    assert len(lines) == 1, "Duplicate content must be skipped"


async def test_different_content_both_written(tmp_path: Path):
    """Two articles with different content: both are written."""
    article1 = _make_article(url="https://site.com/a", content="Unique content A.")
    article2 = _make_article(url="https://site.com/b", content="Unique content B.")

    sink = JsonlSink(tmp_path / "output")
    await sink.write(_make_result(article1))
    await sink.write(_make_result(article2))
    await sink.close()

    lines = [ln for ln in sink.path.read_text(encoding="utf-8").strip().split("\n") if ln]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# JsonlSink — crash-safe resume
# ---------------------------------------------------------------------------


async def test_resume_seen_reflects_prior_manifest(tmp_path: Path):
    """A new JsonlSink on the same path resumes: seen() reflects the old manifest."""
    url = "https://example.com/resume-test"
    output = tmp_path / "run"

    # First run: write and close.
    sink1 = JsonlSink(output)
    await sink1.write(_make_result(_make_article(url=url)))
    await sink1.close()

    # Second run: fresh sink, same path.
    sink2 = JsonlSink(output)
    assert sink2.seen(url) is True


async def test_resume_does_not_double_write(tmp_path: Path):
    """A URL already in the manifest is not written again on resume."""
    url = "https://example.com/no-double"
    output = tmp_path / "run"

    sink1 = JsonlSink(output)
    await sink1.write(_make_result(_make_article(url=url)))
    await sink1.close()

    # The second run must NOT rewrite the same URL (seen() True → caller skips).
    sink2 = JsonlSink(output)
    # Simulate caller obeying seen():
    if not sink2.seen(url):
        await sink2.write(_make_result(_make_article(url=url)))
    await sink2.close()

    lines = [ln for ln in sink2.path.read_text(encoding="utf-8").strip().split("\n") if ln]
    assert len(lines) == 1, "Each URL must appear at most once in the JSONL"


# ---------------------------------------------------------------------------
# JsonlSink — JSONL field contract
# ---------------------------------------------------------------------------


async def test_write_includes_required_fields(tmp_path: Path):
    """Each JSONL line contains id, url, title, content, author, publish_date, metadata."""
    pub = datetime(2024, 1, 15, tzinfo=UTC)
    article = Article(
        url="https://example.com/full",
        title="Full Article",
        content="Full body text.",
        author="Jane Doe",
        publish_date=pub,
        metadata={"bucket": "public"},
    )
    sink = JsonlSink(tmp_path / "output")
    await sink.write(_make_result(article))
    await sink.close()

    data = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert data["id"] == url_id("https://example.com/full")
    assert data["url"] == "https://example.com/full"
    assert data["title"] == "Full Article"
    assert data["content"] == "Full body text."
    assert data["author"] == "Jane Doe"
    assert data["publish_date"] is not None
    assert data["metadata"] == {"bucket": "public"}


async def test_write_publish_date_none_is_none_in_json(tmp_path: Path):
    """publish_date=None is serialized as null / None in the JSON line."""
    article = _make_article()  # no publish_date
    sink = JsonlSink(tmp_path / "output")
    await sink.write(_make_result(article))
    await sink.close()

    data = json.loads(sink.path.read_text(encoding="utf-8").strip())
    assert data["publish_date"] is None
