"""ObjectStore port — abstract S3-shaped interface for binary object storage.

This module defines the contract that all object-store backends must implement.
The in-memory implementation lives in ``scrapeforge.core.objectstore.memory``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from scrapeforge.exceptions import ScrapeForgeError


class ObjectStoreError(ScrapeForgeError):
    """Object-store failure: put, get, exists, or delete error."""


class ObjectNotFound(ObjectStoreError):
    """Raised by ``get()`` when the requested key does not exist in the store."""


class ObjectStore(ABC):
    """Abstract base class (port) for all object-store backends.

    The interface is shaped after S3 / MinIO: keys are opaque strings; values are
    raw bytes with an optional content-type hint.  Concrete implementations may back
    this with MinIO, S3, GCS, or an in-memory dict.
    """

    @abstractmethod
    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Store *data* under *key*.  Overwrites any existing value (idempotent)."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Return the bytes stored under *key*.

        Raises
        ------
        ObjectNotFound
            If *key* does not exist in the store.
        """

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Return ``True`` if *key* exists in the store, ``False`` otherwise."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove *key* from the store.  Idempotent — no error if *key* is absent."""
