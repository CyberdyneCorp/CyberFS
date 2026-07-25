"""Cache port.

Deliberately narrow: string keys, JSON-serializable values, explicit TTLs, and
prefix deletion for subtree invalidation. Nothing here implies Redis.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol


class Cache(Protocol):
    """An accelerator. Every method may fail without breaking correctness."""

    @property
    def available(self) -> bool:
        """Whether the cache is currently usable.

        False while a circuit breaker is open; the caller then goes straight to
        Postgres rather than paying a timeout per request.
        """
        ...

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl: timedelta) -> None:
        """Store with a finite TTL. There is no indefinite write."""
        ...

    async def delete(self, *keys: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int:
        """Drop every key under `prefix`, returning how many.

        Used for subtree permission invalidation and for admin purges.
        """
        ...

    async def ping(self) -> bool: ...

    async def stats(self) -> dict[str, Any]:
        """Key counts and memory use -- never cached values themselves."""
        ...
