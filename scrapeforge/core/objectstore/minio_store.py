"""MinioStore — S3-compatible ObjectStore implementation via aioboto3.

Connects to MinIO (or real AWS S3 / GCS via S3 gateway) using the S3 API.
The adapter is intentionally thin: one responsibility is translating the
abstract ``ObjectStore`` port operations into ``aioboto3`` S3 client calls.

Configuration
-------------
Supply ``endpoint_url`` for MinIO / any S3-compatible service.  Pass
``endpoint_url=None`` (or omit) to hit real AWS S3 (no custom endpoint).

Error mapping
-------------
- ``get()``    — ``NoSuchKey`` / HTTP-404 ``ClientError`` → ``ObjectNotFound``
- ``exists()`` — HTTP-404 ``ClientError`` → returns ``False``
- ``delete()`` — S3 delete of a missing key is a no-op success (idempotent by spec)
- All other ``ClientError`` instances propagate as-is (not swallowed).

Async contract
--------------
All I/O is ``async``.  The aioboto3 session is created eagerly in ``__init__``
(no I/O); the S3 client is created fresh per-operation via the
``_s3_client()`` async context manager so we never hold a long-lived connection
object across await points that could be garbage-collected.

Example::

    store = MinioStore(
        endpoint_url="http://localhost:9000",
        bucket="scrapeforge-raw",
        access_key="minioadmin",
        secret_key="minioadmin",
    )
    await store.ensure_bucket()
    await store.put("raw/2024/article.html", html_bytes, "text/html")
    data = await store.get("raw/2024/article.html")
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aioboto3
from botocore.exceptions import ClientError

from scrapeforge.core.objectstore.base import ObjectNotFound, ObjectStore


class MinioStore(ObjectStore):
    """S3-compatible object store backed by aioboto3 (works with MinIO or AWS S3).

    Parameters
    ----------
    endpoint_url:
        Custom S3 endpoint (e.g. ``"http://localhost:9000"`` for MinIO).
        Pass ``None`` or ``""`` to use the default AWS endpoint.
    bucket:
        Bucket name.  Create it first with ``ensure_bucket()`` or provision it
        externally.
    access_key:
        AWS / MinIO access key id.
    secret_key:
        AWS / MinIO secret access key.
    region:
        AWS region name (default ``"us-east-1"``).
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._endpoint_url = endpoint_url or None  # normalise "" → None
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    # ------------------------------------------------------------------
    # Class-method constructor from Settings
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: object) -> MinioStore:
        """Build a ``MinioStore`` from the shared ``Settings`` object.

        Reads ``OBJECT_STORE_*`` fields from *settings* (duck-typed — any object
        with the expected attributes works).
        """
        return cls(
            endpoint_url=getattr(settings, "OBJECT_STORE_ENDPOINT", None) or None,
            bucket=settings.OBJECT_STORE_BUCKET,  # type: ignore[attr-defined]
            access_key=settings.OBJECT_STORE_ACCESS_KEY,  # type: ignore[attr-defined]
            secret_key=settings.OBJECT_STORE_SECRET_KEY,  # type: ignore[attr-defined]
            region=getattr(settings, "OBJECT_STORE_REGION", "us-east-1"),
        )

    # ------------------------------------------------------------------
    # Private async context manager — yields a fresh S3 client
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _s3_client(self) -> AsyncIterator[object]:
        """Yield a short-lived aioboto3 S3 client.

        ``endpoint_url`` is omitted from the call if it is ``None`` so the
        standard AWS endpoint resolver kicks in (real S3 / GCS-compatible).
        """
        kwargs: dict[str, object] = {
            "aws_access_key_id": self._access_key,
            "aws_secret_access_key": self._secret_key,
            "region_name": self._region,
        }
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url

        async with self._session.client("s3", **kwargs) as client:
            yield client

    # ------------------------------------------------------------------
    # ObjectStore abstract method implementations
    # ------------------------------------------------------------------

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Store *data* under *key*.  Overwrites any existing value (idempotent).

        Parameters
        ----------
        key:
            Object key (path) within the bucket.
        data:
            Raw bytes to store.
        content_type:
            MIME type hint stored as object metadata.
        """
        async with self._s3_client() as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

    async def get(self, key: str) -> bytes:
        """Return the bytes stored under *key*.

        Raises
        ------
        ObjectNotFound
            If *key* does not exist in the bucket (``NoSuchKey`` or HTTP 404).
        """
        async with self._s3_client() as s3:
            try:
                resp = await s3.get_object(Bucket=self._bucket, Key=key)
                return await resp["Body"].read()
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("NoSuchKey", "404"):
                    raise ObjectNotFound(f"Object not found: {key!r}") from exc
                raise

    async def exists(self, key: str) -> bool:
        """Return ``True`` if *key* exists in the bucket, ``False`` otherwise.

        Uses ``HeadObject`` — cheaper than ``GetObject`` (no data transfer).
        """
        async with self._s3_client() as s3:
            try:
                await s3.head_object(Bucket=self._bucket, Key=key)
                return True
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("NoSuchKey", "404"):
                    return False
                raise

    async def delete(self, key: str) -> None:
        """Remove *key* from the bucket.

        Idempotent — S3 returns success even if the key does not exist, so no
        ``ObjectNotFound`` is raised for missing keys.
        """
        async with self._s3_client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key)

    # ------------------------------------------------------------------
    # Convenience initialisation helper
    # ------------------------------------------------------------------

    async def ensure_bucket(self) -> None:
        """Create the bucket if it does not already exist.

        Uses ``HeadBucket`` to check; on a 404 ``ClientError`` calls
        ``CreateBucket``.  Safe to call multiple times (idempotent).

        Note on CreateBucket and LocationConstraint: AWS S3 requires a
        ``CreateBucketConfiguration`` for regions other than ``us-east-1``.
        MinIO ignores it.  We include it only for non-default regions to keep
        the adapter compatible with both.
        """
        async with self._s3_client() as s3:
            try:
                await s3.head_bucket(Bucket=self._bucket)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("NoSuchBucket", "404"):
                    create_kwargs: dict[str, object] = {"Bucket": self._bucket}
                    if self._region != "us-east-1":
                        create_kwargs["CreateBucketConfiguration"] = {
                            "LocationConstraint": self._region
                        }
                    await s3.create_bucket(**create_kwargs)
                else:
                    raise
