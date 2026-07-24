"""Cache policy: what is cached, under what key, for how long.

Redis is an accelerator and never the system of record. Everything here is
derived data that can be recomputed from Postgres, so losing the whole cache
costs latency and nothing else.

The dangerous failure mode is serving a *revoked* permission, which is why
invalidation is synchronous on the write path and the TTL exists only to
self-heal a miss caused by some future bug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

NAMESPACE = "cyberfs"


class Dataset(StrEnum):
    """Exactly what may be cached. Anything absent here is read from Postgres.

    Audit records and grant listings are deliberately missing: they are read
    rarely, must never be stale in an investigation, and caching them would
    add risk for no measurable gain.
    """

    PERMISSION = "perm"
    METADATA = "meta"
    LISTING = "list"
    JWKS = "jwks"
    QUOTA = "quota"
    ADMIN_STATS = "stats"


#: Datasets whose value depends on *who* is asking. Their keys must carry the
#: subject, or one principal's answer could be served to another.
SUBJECT_SCOPED = frozenset({Dataset.PERMISSION, Dataset.LISTING})


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """TTL per dataset. Every entry expires; nothing is stored indefinitely."""

    permission: timedelta = timedelta(seconds=60)
    metadata: timedelta = timedelta(seconds=300)
    listing: timedelta = timedelta(seconds=120)
    jwks: timedelta = timedelta(seconds=3600)
    quota: timedelta = timedelta(seconds=300)
    admin_stats: timedelta = timedelta(seconds=600)

    def ttl_for(self, dataset: Dataset) -> timedelta:
        return {
            Dataset.PERMISSION: self.permission,
            Dataset.METADATA: self.metadata,
            Dataset.LISTING: self.listing,
            Dataset.JWKS: self.jwks,
            Dataset.QUOTA: self.quota,
            Dataset.ADMIN_STATS: self.admin_stats,
        }[dataset]


def cache_key(schema_version: int, dataset: Dataset, *parts: object) -> str:
    """`cyberfs:v<schema>:<dataset>:<parts…>`.

    Bumping the schema version on deploy makes every previously cached entry
    unreachable, which is how a shape change is rolled out without a flush.
    """
    tail = ":".join(str(part) for part in parts)
    return f"{NAMESPACE}:v{schema_version}:{dataset}:{tail}"


def dataset_prefix(schema_version: int, dataset: Dataset) -> str:
    """Everything under one dataset, for a targeted purge."""
    return f"{NAMESPACE}:v{schema_version}:{dataset}:"


@dataclass
class CircuitBreaker:
    """Stops hammering a cache that is failing.

    Opens after sustained failure and closes again on its own, so a Redis
    outage costs one timeout rather than one per request, and recovery needs no
    restart.
    """

    trip_after: timedelta
    cooldown: timedelta
    _failing_since: datetime | None = field(default=None, repr=False)
    _opened_at: datetime | None = field(default=None, repr=False)

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allows(self, now: datetime) -> bool:
        if self._opened_at is None:
            return True
        if now - self._opened_at >= self.cooldown:
            # Let one request through to see whether the cache is back.
            self._opened_at = None
            self._failing_since = None
            return True
        return False

    def record_success(self) -> None:
        self._failing_since = None
        self._opened_at = None

    def record_failure(self, now: datetime) -> None:
        if self._failing_since is None:
            self._failing_since = now
            return
        if now - self._failing_since >= self.trip_after:
            self._opened_at = now
