"""Tests for core/objectstore/minio_store.py — MinioStore implementation.

Hermetic approach: botocore Stubber + _s3_client monkeypatch
-------------------------------------------------------------
moto's in-process mock (mock_aws) does NOT intercept aiobotocore 2.25.1 calls:
aiobotocore uses aiohttp which bypasses moto's urllib3-level patches.

moto's ThreadedMotoServer (real HTTP in-process) requires ``flask`` which is not
a declared dependency in this project's venv.

Instead we use botocore's built-in ``Stubber`` (pure Python, no network, no extra
deps) together with a tiny ``AsyncBytesIO`` helper so that ``get_object``'s Body
can be awaited.  ``_s3_client()`` is monkeypatched per-test to yield the single
stubbed client rather than opening a new connection on each call.

This is fully hermetic: no network, no Docker, no external processes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager

import aioboto3
import pytest
from botocore.stub import Stubber

from scrapeforge.core.objectstore.base import ObjectNotFound
from scrapeforge.core.objectstore.minio_store import MinioStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUCKET = "scrapeforge-test"
_FAKE_ENDPOINT = "http://fake-s3-endpoint:9999"


class AsyncBytesIO:
    """Minimal async-readable wrapper around bytes.

    botocore's Stubber returns ``BytesIO`` objects for ``Body`` but
    aiobotocore's production ``StreamingBody.read()`` is a coroutine.
    This thin wrapper makes the body awaitable so ``MinioStore.get()``
    works correctly under Stubber.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:  # noqa: D102
        return self._data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def _persistent_s3_client() -> AsyncGenerator[tuple[object, Stubber], None]:
    """Module-scoped aiobotocore S3 client with an attached Stubber.

    Opening aioboto3 clients is cheap but not zero-cost; reuse one across
    all tests in this module.  The Stubber is activated here and lives for
    the whole module.
    """
    session = aioboto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    client_ctx = session.client("s3", endpoint_url=_FAKE_ENDPOINT)
    client = await client_ctx.__aenter__()
    stubber = Stubber(client)
    stubber.activate()
    yield client, stubber
    stubber.deactivate()
    await client_ctx.__aexit__(None, None, None)


@pytest.fixture
def store(
    _persistent_s3_client: tuple[object, Stubber],
) -> Generator[tuple[MinioStore, Stubber], None, None]:
    """Yield a (MinioStore, Stubber) pair for a single test.

    ``_s3_client`` is monkeypatched so that every ``async with store._s3_client()``
    returns the same stubbed client instead of opening a real network connection.
    After each test, assert no pending stub responses were left unconsumed.
    """
    client, stubber = _persistent_s3_client

    @asynccontextmanager
    async def _patched_s3_client():
        yield client

    s = MinioStore(
        endpoint_url=_FAKE_ENDPOINT,
        bucket=_BUCKET,
        access_key="testing",
        secret_key="testing",
    )
    s._s3_client = _patched_s3_client  # type: ignore[method-assign]

    yield s, stubber

    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# ensure_bucket
# ---------------------------------------------------------------------------


async def test_ensure_bucket_creates_when_missing(
    store: tuple[MinioStore, Stubber],
) -> None:
    """ensure_bucket() calls CreateBucket when HeadBucket returns 404."""
    s, stubber = store
    stubber.add_client_error(
        "head_bucket",
        service_error_code="NoSuchBucket",
        http_status_code=404,
        expected_params={"Bucket": _BUCKET},
    )
    stubber.add_response("create_bucket", {}, {"Bucket": _BUCKET})
    await s.ensure_bucket()


async def test_ensure_bucket_is_idempotent(
    store: tuple[MinioStore, Stubber],
) -> None:
    """ensure_bucket() is a no-op when the bucket already exists."""
    s, stubber = store
    stubber.add_response("head_bucket", {}, {"Bucket": _BUCKET})
    await s.ensure_bucket()  # must not raise or call CreateBucket


# ---------------------------------------------------------------------------
# put / get round-trips
# ---------------------------------------------------------------------------


async def test_put_get_roundtrip(store: tuple[MinioStore, Stubber]) -> None:
    """put() then get() returns the same bytes."""
    s, stubber = store
    data = b"hello, MinioStore!"
    key = "test/roundtrip.bin"
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": _BUCKET,
            "Key": key,
            "Body": data,
            "ContentType": "application/octet-stream",
        },
    )
    await s.put(key, data)
    stubber.add_response(
        "get_object",
        {
            "Body": AsyncBytesIO(data),
            "ContentLength": len(data),
            "ContentType": "application/octet-stream",
        },
        {"Bucket": _BUCKET, "Key": key},
    )
    result = await s.get(key)
    assert result == data


async def test_put_respects_content_type(store: tuple[MinioStore, Stubber]) -> None:
    """put() passes the provided content_type to S3."""
    s, stubber = store
    data = b"<html/>"
    key = "test/page.html"
    stubber.add_response(
        "put_object",
        {},
        {"Bucket": _BUCKET, "Key": key, "Body": data, "ContentType": "text/html"},
    )
    await s.put(key, data, content_type="text/html")  # must not raise


async def test_put_overwrite(store: tuple[MinioStore, Stubber]) -> None:
    """put() on an existing key overwrites the old value."""
    s, stubber = store
    key = "test/overwrite.bin"
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": _BUCKET,
            "Key": key,
            "Body": b"version 1",
            "ContentType": "application/octet-stream",
        },
    )
    await s.put(key, b"version 1")
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": _BUCKET,
            "Key": key,
            "Body": b"version 2",
            "ContentType": "application/octet-stream",
        },
    )
    stubber.add_response(
        "get_object",
        {
            "Body": AsyncBytesIO(b"version 2"),
            "ContentLength": 9,
            "ContentType": "application/octet-stream",
        },
        {"Bucket": _BUCKET, "Key": key},
    )
    await s.put(key, b"version 2")
    result = await s.get(key)
    assert result == b"version 2"


# ---------------------------------------------------------------------------
# get missing key raises ObjectNotFound
# ---------------------------------------------------------------------------


async def test_get_missing_raises_object_not_found(
    store: tuple[MinioStore, Stubber],
) -> None:
    """get() on a non-existent key raises ObjectNotFound."""
    s, stubber = store
    key = "test/does-not-exist.bin"
    stubber.add_client_error(
        "get_object",
        service_error_code="NoSuchKey",
        http_status_code=404,
        expected_params={"Bucket": _BUCKET, "Key": key},
    )
    with pytest.raises(ObjectNotFound):
        await s.get(key)


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


async def test_exists_true_after_put(store: tuple[MinioStore, Stubber]) -> None:
    """exists() returns True for a key that is present."""
    s, stubber = store
    key = "test/exists-true.bin"
    stubber.add_response("head_object", {}, {"Bucket": _BUCKET, "Key": key})
    assert await s.exists(key) is True


async def test_exists_false_for_missing(store: tuple[MinioStore, Stubber]) -> None:
    """exists() returns False for a key that has never been put."""
    s, stubber = store
    key = "test/does-not-exist.bin"
    stubber.add_client_error(
        "head_object",
        service_error_code="NoSuchKey",
        http_status_code=404,
        expected_params={"Bucket": _BUCKET, "Key": key},
    )
    assert await s.exists(key) is False


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_removes_key(store: tuple[MinioStore, Stubber]) -> None:
    """After delete(), exists() returns False."""
    s, stubber = store
    key = "test/delete-me.bin"
    # put
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": _BUCKET,
            "Key": key,
            "Body": b"to be deleted",
            "ContentType": "application/octet-stream",
        },
    )
    # exists (True)
    stubber.add_response("head_object", {}, {"Bucket": _BUCKET, "Key": key})
    # delete
    stubber.add_response("delete_object", {}, {"Bucket": _BUCKET, "Key": key})
    # exists (False)
    stubber.add_client_error(
        "head_object",
        service_error_code="NoSuchKey",
        http_status_code=404,
        expected_params={"Bucket": _BUCKET, "Key": key},
    )
    await s.put(key, b"to be deleted")
    assert await s.exists(key) is True
    await s.delete(key)
    assert await s.exists(key) is False


async def test_delete_missing_is_noop(store: tuple[MinioStore, Stubber]) -> None:
    """delete() on a non-existent key does not raise (S3 is idempotent)."""
    s, stubber = store
    key = "test/never-existed.bin"
    stubber.add_response("delete_object", {}, {"Bucket": _BUCKET, "Key": key})
    await s.delete(key)  # must not raise
