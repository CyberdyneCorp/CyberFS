"""Typed cache access, with invalidation the write path can rely on.

Reads degrade silently: a broken cache is a miss, and the caller falls through
to Postgres. Invalidation does not degrade -- if Redis is reachable but refuses
to drop a key, the write fails rather than leaving a stale *allow* cached where
a revocation should be.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from cyberfs.domain.cache import CachePolicy, Dataset, cache_key, dataset_prefix
from cyberfs.domain.errors import CacheUnavailableError, DependencyUnavailableError
from cyberfs.domain.ports.cache import Cache
from cyberfs.domain.sharing import Role
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)


class CacheService:
    def __init__(
        self, cache: Cache, *, schema_version: int, policy: CachePolicy | None = None
    ) -> None:
        self._cache = cache
        self._schema = schema_version
        self._policy = policy or CachePolicy()
        #: Coalesces concurrent misses so a hot key is recomputed once.
        self._inflight: dict[str, asyncio.Future[Any]] = {}

    @property
    def available(self) -> bool:
        return self._cache.available

    # --- generic -------------------------------------------------------

    async def get_or_load(
        self, dataset: Dataset, parts: tuple[object, ...], load: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return the cached value, or compute it once and store it.

        Concurrent misses on the same key wait on one computation rather than
        each hitting Postgres -- otherwise a hot key turns a cache expiry into
        a stampede.
        """
        key = cache_key(self._schema, dataset, *parts)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await load()
            await self._cache.set(key, value, self._policy.ttl_for(dataset))
            future.set_result(value)
            return value
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)

    async def _invalidate(self, *keys: str) -> None:
        """Drop keys, failing the caller if a reachable cache refuses."""
        if not keys:
            return
        try:
            await self._cache.delete(*keys)
        except CacheUnavailableError as exc:
            if self._cache.available:
                logger.error("cache_invalidation_failed", keys=len(keys))
                raise DependencyUnavailableError(
                    "could not invalidate the cache; refusing to leave stale state"
                ) from exc
            # The cache is already known-down: nothing is being served from it,
            # so there is no stale entry to worry about.

    async def _invalidate_prefix(self, prefix: str) -> None:
        try:
            await self._cache.delete_prefix(prefix)
        except CacheUnavailableError as exc:
            if self._cache.available:
                raise DependencyUnavailableError(
                    "could not invalidate the cache; refusing to leave stale state"
                ) from exc

    # --- permissions ---------------------------------------------------

    async def permission(
        self, subject: str, node_id: uuid.UUID, load: Callable[[], Awaitable[Role | None]]
    ) -> Role | None:
        """A caller's effective role, cached per subject and node."""

        async def load_ordinal() -> int:
            role = await load()
            # `0` encodes "no access", which is a real answer worth caching:
            # repeated probes by an unauthorized caller should not each walk
            # the ancestor chain.
            return int(role) if role is not None else 0

        ordinal = await self.get_or_load(Dataset.PERMISSION, (subject, node_id), load_ordinal)
        return Role(ordinal) if ordinal else None

    async def invalidate_permissions(self, subject: str, node_id: uuid.UUID) -> None:
        await self._invalidate(cache_key(self._schema, Dataset.PERMISSION, subject, node_id))

    async def invalidate_permissions_for_subject(self, subject: str) -> None:
        """Every decision for one caller.

        Used on a grant change: the affected subtree may be large, and dropping
        one subject's decisions is cheaper and safer than enumerating nodes.
        """
        await self._invalidate_prefix(
            f"{dataset_prefix(self._schema, Dataset.PERMISSION)}{subject}:"
        )

    async def invalidate_all_permissions(self) -> None:
        """Used when a move reparents a subtree, changing inherited access for
        everyone who held a grant above or below it."""
        await self._invalidate_prefix(dataset_prefix(self._schema, Dataset.PERMISSION))

    # --- listings and metadata -----------------------------------------

    async def invalidate_listing(self, parent_id: uuid.UUID | None) -> None:
        if parent_id is None:
            return
        await self._invalidate_prefix(
            f"{dataset_prefix(self._schema, Dataset.LISTING)}{parent_id}:"
        )

    async def invalidate_node(self, node_id: uuid.UUID) -> None:
        await self._invalidate(cache_key(self._schema, Dataset.METADATA, node_id))

    async def invalidate_quota(self, user_id: uuid.UUID) -> None:
        await self._invalidate(cache_key(self._schema, Dataset.QUOTA, user_id))

    async def on_node_mutated(
        self,
        node_id: uuid.UUID,
        *,
        old_parent: uuid.UUID | None = None,
        new_parent: uuid.UUID | None = None,
    ) -> None:
        """Everything a create, rename, move, or delete can affect."""
        await self.invalidate_node(node_id)
        await self.invalidate_listing(old_parent)
        if new_parent != old_parent:
            await self.invalidate_listing(new_parent)

    # --- administration ------------------------------------------------

    async def purge(self, dataset: Dataset) -> int:
        try:
            return await self._cache.delete_prefix(dataset_prefix(self._schema, dataset))
        except CacheUnavailableError:
            return 0

    async def stats(self) -> dict[str, Any]:
        return await self._cache.stats()
