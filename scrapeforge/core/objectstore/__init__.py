"""Object-store port — abstract S3-shaped interface and in-memory implementation.

Exports the public surface so callers can do::

    from scrapeforge.core.objectstore import (
        InMemoryObjectStore, ObjectNotFound, ObjectStore, ObjectStoreError
    )
"""

from scrapeforge.core.objectstore.base import ObjectNotFound, ObjectStore, ObjectStoreError
from scrapeforge.core.objectstore.memory import InMemoryObjectStore

__all__ = ["ObjectNotFound", "ObjectStore", "ObjectStoreError", "InMemoryObjectStore"]
