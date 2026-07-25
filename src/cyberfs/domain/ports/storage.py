"""Object storage port.

Bytes live in MinIO; metadata lives in Postgres. Everything here streams: the
service must never hold a whole object in memory, whatever its size.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredObject:
    """What a listing knows about an object.

    `last_modified` is what lets the reaper honour its grace period: an
    upload whose row is not yet committed must not be collected out from
    under itself.
    """

    key: str
    size: int
    last_modified: datetime | None = None


class ObjectStore(Protocol):
    """Content-addressed by opaque key. Knows nothing about the tree."""

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> int:
        """Stream `data` to `key`, returning the number of bytes stored."""
        ...

    def get(self, key: str, *, offset: int = 0, length: int | None = None) -> AsyncIterator[bytes]:
        """Stream the object back, optionally a byte range of it.

        Not `async def`: the caller iterates the result, so the request is
        issued lazily and a range read never fetches more than it needs.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove an object. Deleting a missing key is not an error."""
        ...

    async def copy(self, source_key: str, target_key: str) -> int:
        """Duplicate an object server-side, returning its size.

        Server-side so a copy never transits the API, and never has to be
        decrypted and re-encrypted -- a copy of an encrypted file is a copy of
        its ciphertext.
        """
        ...

    async def stat(self, key: str) -> int | None:
        """Size in bytes, or None when the key does not exist."""
        ...

    def list_keys(self, prefix: str = "") -> AsyncIterator[StoredObject]:
        """Every object under `prefix`. Drives the orphan reaper."""
        ...

    async def ensure_bucket(self) -> None:
        """Create the bucket if absent, private and versioned."""
        ...
