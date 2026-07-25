"""Redis cache adapter.

Every operation is bounded by a short timeout and guarded by a circuit
breaker: a Redis outage must cost one timeout, not one per request, and must
never turn into a 5xx. Failures are swallowed on the read path and surfaced on
the invalidation path, because a missed invalidation can leave a revoked
permission cached.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.cache import CircuitBreaker
from cyberfs.domain.errors import CacheUnavailableError
from cyberfs.infrastructure.logging import get_logger
from cyberfs.infrastructure.metrics import cache_operations_total

logger = get_logger(__name__)

SCAN_BATCH = 500


class RedisCache:
    """Implements the `Cache` port."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        operation_timeout: timedelta,
        circuit_trip_after: timedelta,
        circuit_cooldown: timedelta | None = None,
    ) -> None:
        self._client = client
        self._timeout = operation_timeout.total_seconds()
        self._breaker = CircuitBreaker(
            trip_after=circuit_trip_after,
            cooldown=circuit_cooldown or circuit_trip_after,
        )

    @property
    def available(self) -> bool:
        return self._breaker.allows(utcnow())

    async def _run(self, name: str, coro: Any) -> Any:
        """Execute with a fast timeout, feeding the breaker either way."""
        if not self._breaker.allows(utcnow()):
            cache_operations_total.labels(dataset=name, outcome="circuit_open").inc()
            raise CacheUnavailableError("cache circuit is open")
        try:
            async with asyncio.timeout(self._timeout):
                result = await coro
        except (TimeoutError, RedisError, OSError) as exc:
            self._breaker.record_failure(utcnow())
            outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
            cache_operations_total.labels(dataset=name, outcome=outcome).inc()
            raise CacheUnavailableError(str(exc)) from exc
        self._breaker.record_success()
        return result

    async def get(self, key: str) -> Any | None:
        dataset = _dataset_of(key)
        try:
            raw = await self._run(dataset, self._client.get(key))
        except CacheUnavailableError:
            # A read miss against a broken cache is just a miss.
            return None
        if raw is None:
            cache_operations_total.labels(dataset=dataset, outcome="miss").inc()
            return None
        cache_operations_total.labels(dataset=dataset, outcome="hit").inc()
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # A value we cannot parse is a stale shape; treat it as absent.
            return None

    async def set(self, key: str, value: Any, ttl: timedelta) -> None:
        try:
            await self._run(
                _dataset_of(key),
                self._client.set(key, json.dumps(value), ex=int(ttl.total_seconds())),
            )
        except CacheUnavailableError:
            # Failing to populate the cache costs latency, never correctness.
            return

    async def delete(self, *keys: str) -> None:
        """Invalidation. Failure propagates: a stale allow must not survive."""
        if not keys:
            return
        await self._run(_dataset_of(keys[0]), self._client.delete(*keys))

    async def delete_prefix(self, prefix: str) -> int:
        removed = 0
        cursor = 0
        while True:
            cursor, batch = await self._run(
                _dataset_of(prefix),
                self._client.scan(cursor=cursor, match=f"{prefix}*", count=SCAN_BATCH),
            )
            if batch:
                await self._run(_dataset_of(prefix), self._client.delete(*batch))
                removed += len(batch)
            if cursor == 0:
                return removed

    async def ping(self) -> bool:
        try:
            await self._run("ping", self._client.ping())
        except CacheUnavailableError:
            return False
        return True

    async def stats(self) -> dict[str, Any]:
        """Counts and memory only -- never the cached values."""
        try:
            info = await self._run("stats", self._client.info("memory"))
            keys = await self._run("stats", self._client.dbsize())
        except CacheUnavailableError:
            return {"available": False}
        return {
            "available": True,
            "keys": int(keys or 0),
            "memory_bytes": int(info.get("used_memory", 0)),
        }


class NullCache:
    """Used when caching is switched off. Every read is a miss."""

    @property
    def available(self) -> bool:
        return False

    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, ttl: timedelta) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        return None

    async def delete_prefix(self, prefix: str) -> int:
        return 0

    async def ping(self) -> bool:
        return False

    async def stats(self) -> dict[str, Any]:
        return {"available": False, "disabled": True}


def _dataset_of(key: str) -> str:
    """Pull the dataset out of a key for per-dataset metrics."""
    parts = key.split(":", 3)
    return parts[2] if len(parts) > 2 else "unknown"
