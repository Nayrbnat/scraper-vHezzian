"""Tests for core/objectstore — ObjectStore port and InMemoryObjectStore implementation.

Written FIRST (TDD red phase). All tests must fail before implementation exists.
Uses asyncio_mode=auto (configured in pyproject.toml), so no explicit @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import pytest

from scrapeforge.core.objectstore.base import ObjectNotFound, ObjectStore, ObjectStoreError
from scrapeforge.core.objectstore.memory import InMemoryObjectStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryObjectStore:
    """Fresh InMemoryObjectStore."""
    return InMemoryObjectStore()


# ---------------------------------------------------------------------------
# put + get round-trip
# ---------------------------------------------------------------------------


async def test_put_get_roundtrip_bytes(store: InMemoryObjectStore) -> None:
    """put() then get() returns the exact same bytes."""
    data = b"hello scrapeforge"
    await store.put("my/key.html", data)
    result = await store.get("my/key.html")
    assert result == data


async def test_put_get_binary(store: InMemoryObjectStore) -> None:
    """put/get works with arbitrary binary content."""
    data = bytes(range(256))
    await store.put("bin/file.bin", data, content_type="application/octet-stream")
    result = await store.get("bin/file.bin")
    assert result == data


# ---------------------------------------------------------------------------
# get missing key → ObjectNotFound
# ---------------------------------------------------------------------------


async def test_get_missing_raises_object_not_found(store: InMemoryObjectStore) -> None:
    """get() raises ObjectNotFound when the key does not exist."""
    with pytest.raises(ObjectNotFound):
        await store.get("does/not/exist.html")


async def test_object_not_found_is_object_store_error(store: InMemoryObjectStore) -> None:
    """ObjectNotFound is a subclass of ObjectStoreError."""
    with pytest.raises(ObjectStoreError):
        await store.get("missing")


# ---------------------------------------------------------------------------
# exists True / False
# ---------------------------------------------------------------------------


async def test_exists_true(store: InMemoryObjectStore) -> None:
    """exists() returns True for a key that has been put."""
    await store.put("k", b"v")
    assert await store.exists("k") is True


async def test_exists_false(store: InMemoryObjectStore) -> None:
    """exists() returns False for a key that has not been put."""
    assert await store.exists("nope") is False


async def test_exists_false_after_delete(store: InMemoryObjectStore) -> None:
    """exists() returns False after the key is deleted."""
    await store.put("k", b"v")
    await store.delete("k")
    assert await store.exists("k") is False


# ---------------------------------------------------------------------------
# put overwrites (idempotent)
# ---------------------------------------------------------------------------


async def test_put_overwrites(store: InMemoryObjectStore) -> None:
    """A second put() on the same key replaces the value."""
    await store.put("k", b"first", content_type="text/plain")
    await store.put("k", b"second", content_type="text/html")
    result = await store.get("k")
    assert result == b"second"


# ---------------------------------------------------------------------------
# delete removes key
# ---------------------------------------------------------------------------


async def test_delete_removes(store: InMemoryObjectStore) -> None:
    """delete() removes the key so subsequent get raises ObjectNotFound."""
    await store.put("k", b"data")
    await store.delete("k")
    with pytest.raises(ObjectNotFound):
        await store.get("k")


async def test_delete_missing_is_noop(store: InMemoryObjectStore) -> None:
    """delete() on a non-existent key does not raise."""
    await store.delete("does/not/exist")  # must not raise


# ---------------------------------------------------------------------------
# content_type is stored (internal state; verified via InMemoryObjectStore internals)
# ---------------------------------------------------------------------------


async def test_put_stores_content_type(store: InMemoryObjectStore) -> None:
    """InMemoryObjectStore stores the content_type alongside the data."""
    await store.put("k", b"data", content_type="text/html")
    # Access internal dict to confirm content_type is stored
    data, ct = store._objects["k"]
    assert ct == "text/html"


async def test_put_default_content_type(store: InMemoryObjectStore) -> None:
    """Default content_type is 'application/octet-stream'."""
    await store.put("k", b"data")
    _, ct = store._objects["k"]
    assert ct == "application/octet-stream"


# ---------------------------------------------------------------------------
# ObjectStoreError hierarchy
# ---------------------------------------------------------------------------


def test_object_store_error_is_scrapeforge_error() -> None:
    """ObjectStoreError inherits from ScrapeForgeError."""
    from scrapeforge.exceptions import ScrapeForgeError

    err = ObjectStoreError("storage broke")
    assert isinstance(err, ScrapeForgeError)


def test_object_not_found_subclasses_object_store_error() -> None:
    """ObjectNotFound inherits from ObjectStoreError (exception hierarchy check)."""
    err = ObjectNotFound("key missing")
    assert isinstance(err, ObjectStoreError)


# ---------------------------------------------------------------------------
# ObjectStore is abstract
# ---------------------------------------------------------------------------


def test_object_store_is_abstract() -> None:
    """ObjectStore cannot be instantiated directly (ABC)."""
    with pytest.raises(TypeError):
        ObjectStore()  # type: ignore[abstract]
