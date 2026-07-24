"""MinIO object store.

`minio-py` is synchronous, so every call runs in a worker thread. Uploads are
the interesting case: the bytes arrive from an async request stream, but the
client wants a blocking file-like object. `_AsyncStreamReader` bridges the two
without buffering the body -- it pulls one chunk at a time from the event loop
on demand, so peak memory is one part, not one file.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from typing import BinaryIO, cast

from minio import Minio
from minio.commonconfig import ENABLED, CopySource
from minio.error import S3Error
from minio.versioningconfig import VersioningConfig

from cyberfs.domain.errors import DependencyUnavailableError, NotFoundError
from cyberfs.domain.ports.storage import StoredObject
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: S3 multipart requires parts of at least 5 MiB (except the last).
MIN_PART_BYTES = 5 * 1024 * 1024
NO_SUCH_KEY = frozenset({"NoSuchKey", "NoSuchObject"})


class _AsyncStreamReader(io.RawIOBase):
    """A blocking file-like view over an async byte iterator.

    Read from a worker thread while the event loop keeps running, so each
    `read` hands control back to the loop to produce the next chunk rather
    than requiring the whole body up front.
    """

    def __init__(self, source: AsyncIterator[bytes], loop: asyncio.AbstractEventLoop) -> None:
        self._source = source
        self._loop = loop
        self._buffer = bytearray()
        self._exhausted = False
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    async def _next_chunk(self) -> bytes:
        try:
            return await anext(self._source)
        except StopAsyncIteration:
            return b""

    def _fill(self, size: int) -> None:
        while not self._exhausted and len(self._buffer) < size:
            future = asyncio.run_coroutine_threadsafe(self._next_chunk(), self._loop)
            chunk = future.result()
            if not chunk:
                self._exhausted = True
                return
            self._buffer.extend(chunk)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            # minio always asks for a bounded amount; an unbounded read would
            # mean buffering the whole body, which is what this class avoids.
            raise ValueError("unbounded read would defeat streaming")
        self._fill(size)
        taken = bytes(self._buffer[:size])
        del self._buffer[: len(taken)]
        self.bytes_read += len(taken)
        return taken


class MinioObjectStore:
    """Implements the `ObjectStore` port."""

    def __init__(
        self,
        client: Minio,
        bucket: str,
        *,
        part_bytes: int = MIN_PART_BYTES,
    ) -> None:
        self._client = client
        self._bucket = bucket
        # Bounds the client's own buffering, and therefore peak memory.
        self._part_bytes = max(part_bytes, MIN_PART_BYTES)

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket(self) -> None:
        """Create the bucket if absent, with private access and versioning on.

        MinIO buckets are private by default (no anonymous policy). Versioning
        is enabled explicitly -- `deployment/spec.md` requires it, and it lets a
        deleted or overwritten object be recovered. `set_bucket_versioning` is
        idempotent, so re-running against an existing bucket is a safe no-op.
        """
        try:
            exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
            if not exists:
                await asyncio.to_thread(self._client.make_bucket, self._bucket)
                logger.info("bucket_created", bucket=self._bucket)
            await asyncio.to_thread(
                self._client.set_bucket_versioning,
                self._bucket,
                VersioningConfig(ENABLED),
            )
        except S3Error as exc:
            raise DependencyUnavailableError("object store is unavailable") from exc

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> int:
        loop = asyncio.get_running_loop()
        reader = _AsyncStreamReader(data, loop)
        try:
            await asyncio.to_thread(
                self._client.put_object,
                self._bucket,
                key,
                # A RawIOBase that only ever serves bounded reads; minio wants
                # the nominal BinaryIO type but uses only `read`.
                cast(BinaryIO, reader),
                length=-1,
                part_size=self._part_bytes,
                content_type=content_type,
            )
        except S3Error as exc:
            raise DependencyUnavailableError("object write failed") from exc
        return reader.bytes_read

    async def _open(self, key: str, offset: int, length: int | None) -> object:
        try:
            return await asyncio.to_thread(
                self._client.get_object,
                self._bucket,
                key,
                offset=offset,
                length=length or 0,
            )
        except S3Error as exc:
            if exc.code in NO_SUCH_KEY:
                raise NotFoundError("stored object is missing", key=key) from exc
            raise DependencyUnavailableError("object read failed") from exc

    async def get(
        self, key: str, *, offset: int = 0, length: int | None = None
    ) -> AsyncIterator[bytes]:
        response = await self._open(key, offset, length)
        try:
            while True:
                chunk = await asyncio.to_thread(response.read, self._part_bytes)  # type: ignore[attr-defined]
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(response.close)  # type: ignore[attr-defined]
            await asyncio.to_thread(response.release_conn)  # type: ignore[attr-defined]

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._client.remove_object, self._bucket, key)
        except S3Error as exc:
            if exc.code in NO_SUCH_KEY:
                return  # Deleting what is already gone is a success.
            raise DependencyUnavailableError("object delete failed") from exc

    async def copy(self, source_key: str, target_key: str) -> int:
        """Server-side copy: the bytes never transit the API."""
        try:
            await asyncio.to_thread(
                self._client.copy_object,
                self._bucket,
                target_key,
                CopySource(self._bucket, source_key),
            )
        except S3Error as exc:
            if exc.code in NO_SUCH_KEY:
                raise NotFoundError("source object is missing", key=source_key) from exc
            raise DependencyUnavailableError("object copy failed") from exc
        return await self.stat(target_key) or 0

    async def stat(self, key: str) -> int | None:
        try:
            info = await asyncio.to_thread(self._client.stat_object, self._bucket, key)
        except S3Error as exc:
            if exc.code in NO_SUCH_KEY:
                return None
            raise DependencyUnavailableError("object stat failed") from exc
        size: int = info.size or 0
        return size

    async def list_keys(self, prefix: str = "") -> AsyncIterator[StoredObject]:
        """Every object under `prefix`. Materialized per page by the client."""

        def _list() -> list[StoredObject]:
            return [
                StoredObject(
                    key=obj.object_name or "",
                    size=obj.size or 0,
                    last_modified=obj.last_modified,
                )
                for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
            ]

        try:
            entries = await asyncio.to_thread(_list)
        except S3Error as exc:
            raise DependencyUnavailableError("object listing failed") from exc
        for entry in entries:
            yield entry
