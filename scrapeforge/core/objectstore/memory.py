"""InMemoryObjectStore — first-class in-process implementation of the ObjectStore port.

Backed by a ``dict[str, tuple[bytes, str]]`` mapping key → (data, content_type).
This is NOT test-only infrastructure — it is a first-class implementation usable for
local / single-process runs and as a drop-in test double for higher-level tests.
"""

from __future__ import annotations

from scrapeforge.core.objectstore.base import ObjectNotFound, ObjectStore


class InMemoryObjectStore(ObjectStore):
    """A fully working in-process ObjectStore backed by a Python dict.

    The internal ``_objects`` attribute is intentionally public (leading underscore
    convention) so tests and monitoring code can inspect stored content-types without
    going through the abstract interface.
    """

    def __init__(self) -> None:
        # key → (data bytes, content_type string)
        self._objects: dict[str, tuple[bytes, str]] = {}

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Store *data* under *key*, overwriting any previous value."""
        self._objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        """Return the bytes stored under *key*.

        Raises
        ------
        ObjectNotFound
            If *key* is not present.
        """
        try:
            data, _ = self._objects[key]
        except KeyError:
            raise ObjectNotFound(f"Object not found: {key!r}") from None
        return data

    async def exists(self, key: str) -> bool:
        """Return ``True`` if *key* exists, ``False`` otherwise."""
        return key in self._objects

    async def delete(self, key: str) -> None:
        """Remove *key* from the store.  No-op if *key* does not exist."""
        self._objects.pop(key, None)
