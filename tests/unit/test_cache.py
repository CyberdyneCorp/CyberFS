"""Cache policy, keys, circuit breaking, and invalidation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cyberfs.application.caching import CacheService
from cyberfs.domain.cache import (
    NAMESPACE,
    SUBJECT_SCOPED,
    CachePolicy,
    CircuitBreaker,
    Dataset,
    cache_key,
    dataset_prefix,
)
from cyberfs.domain.errors import CacheUnavailableError, DependencyUnavailableError
from cyberfs.domain.sharing import Role

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


class FakeCache:
    """In-memory cache with switchable failure, implementing the port."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.ttls: dict[str, timedelta] = {}
        self.fail = False
        self.reachable = True
        self.deletes: list[tuple[str, ...]] = []

    @property
    def available(self) -> bool:
        return self.reachable

    def _guard(self) -> None:
        if self.fail:
            raise CacheUnavailableError("simulated")

    async def get(self, key: str) -> Any | None:
        if self.fail:
            return None  # a broken cache is a miss
        return self.store.get(key)

    async def set(self, key: str, value: Any, ttl: timedelta) -> None:
        if self.fail:
            return
        self.store[key] = value
        self.ttls[key] = ttl

    async def delete(self, *keys: str) -> None:
        self._guard()
        self.deletes.append(keys)
        for key in keys:
            self.store.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        self._guard()
        doomed = [key for key in self.store if key.startswith(prefix)]
        for key in doomed:
            del self.store[key]
        return len(doomed)

    async def ping(self) -> bool:
        return not self.fail

    async def stats(self) -> dict[str, Any]:
        return {"available": not self.fail, "keys": len(self.store)}


def service(cache: FakeCache | None = None, **kw: Any) -> CacheService:
    return CacheService(cache or FakeCache(), schema_version=kw.pop("schema_version", 1), **kw)


# --- keys ------------------------------------------------------------------


def test_a_key_is_namespaced_and_versioned() -> None:
    key = cache_key(3, Dataset.METADATA, "abc")
    assert key == f"{NAMESPACE}:v3:meta:abc"


def test_bumping_the_schema_makes_old_keys_unreachable() -> None:
    """How a shape change is rolled out without a flush."""
    assert cache_key(1, Dataset.METADATA, "abc") != cache_key(2, Dataset.METADATA, "abc")


def test_datasets_do_not_collide() -> None:
    keys = {cache_key(1, dataset, "same") for dataset in Dataset}
    assert len(keys) == len(Dataset)


def test_permission_and_listing_are_subject_scoped() -> None:
    """Anything whose answer depends on the caller must key on the caller."""
    assert Dataset.PERMISSION in SUBJECT_SCOPED
    assert Dataset.LISTING in SUBJECT_SCOPED


def test_two_subjects_get_distinct_permission_keys() -> None:
    node = uuid.uuid4()
    assert cache_key(1, Dataset.PERMISSION, "alice", node) != cache_key(
        1, Dataset.PERMISSION, "bob", node
    )


def test_pagination_is_part_of_a_listing_key() -> None:
    parent = uuid.uuid4()
    first = cache_key(1, Dataset.LISTING, parent, "alice", "cur-a", 50)
    second = cache_key(1, Dataset.LISTING, parent, "alice", "cur-b", 50)
    assert first != second


def test_a_prefix_covers_its_dataset() -> None:
    prefix = dataset_prefix(1, Dataset.PERMISSION)
    assert cache_key(1, Dataset.PERMISSION, "alice", "n").startswith(prefix)
    assert not cache_key(1, Dataset.METADATA, "n").startswith(prefix)


# --- TTLs ------------------------------------------------------------------


def test_every_dataset_has_a_finite_ttl() -> None:
    """Nothing is ever stored indefinitely."""
    policy = CachePolicy()
    for dataset in Dataset:
        assert policy.ttl_for(dataset) > timedelta(0)


def test_the_permission_ttl_is_short() -> None:
    """It is only a backstop; invalidation is what makes revocation immediate."""
    assert CachePolicy().ttl_for(Dataset.PERMISSION) <= timedelta(seconds=60)


async def test_a_stored_value_carries_its_ttl() -> None:
    cache = FakeCache()
    await service(cache).permission("alice", uuid.uuid4(), _returns(Role.VIEWER))
    assert all(ttl > timedelta(0) for ttl in cache.ttls.values())


# --- circuit breaker -------------------------------------------------------


def breaker() -> CircuitBreaker:
    return CircuitBreaker(trip_after=timedelta(seconds=10), cooldown=timedelta(seconds=30))


def test_a_fresh_breaker_allows_traffic() -> None:
    assert breaker().allows(NOW)


def test_one_failure_does_not_trip_it() -> None:
    subject = breaker()
    subject.record_failure(NOW)
    assert subject.allows(NOW)


def test_sustained_failure_opens_it() -> None:
    """So an outage costs one timeout, not one per request."""
    subject = breaker()
    subject.record_failure(NOW)
    subject.record_failure(NOW + timedelta(seconds=11))
    assert subject.is_open
    assert not subject.allows(NOW + timedelta(seconds=11))


def test_success_clears_the_failure_streak() -> None:
    subject = breaker()
    subject.record_failure(NOW)
    subject.record_success()
    subject.record_failure(NOW + timedelta(seconds=11))
    assert not subject.is_open


def test_it_closes_again_after_the_cooldown() -> None:
    """Recovery must need no restart."""
    subject = breaker()
    subject.record_failure(NOW)
    subject.record_failure(NOW + timedelta(seconds=11))
    assert not subject.allows(NOW + timedelta(seconds=20))
    assert subject.allows(NOW + timedelta(seconds=60))


# --- read behaviour --------------------------------------------------------


def _returns(role: Role | None):
    async def load() -> Role | None:
        return role

    return load


def _counting(role: Role | None):
    calls = {"n": 0}

    async def load() -> Role | None:
        calls["n"] += 1
        return role

    return load, calls


async def test_a_permission_is_cached() -> None:
    cache = FakeCache()
    subject = service(cache)
    load, calls = _counting(Role.EDITOR)
    node = uuid.uuid4()

    assert await subject.permission("alice", node, load) is Role.EDITOR
    assert await subject.permission("alice", node, load) is Role.EDITOR
    assert calls["n"] == 1


async def test_no_access_is_cached_too() -> None:
    """Repeated probes by an unauthorized caller should not each walk the tree."""
    cache = FakeCache()
    subject = service(cache)
    load, calls = _counting(None)
    node = uuid.uuid4()

    assert await subject.permission("mallory", node, load) is None
    assert await subject.permission("mallory", node, load) is None
    assert calls["n"] == 1


async def test_one_subjects_answer_is_never_served_to_another() -> None:
    cache = FakeCache()
    subject = service(cache)
    node = uuid.uuid4()

    await subject.permission("alice", node, _returns(Role.OWNER))
    assert await subject.permission("bob", node, _returns(None)) is None


async def test_concurrent_misses_are_coalesced() -> None:
    """A hot key must not turn an expiry into a stampede."""
    import asyncio

    cache = FakeCache()
    subject = service(cache)
    calls = {"n": 0}

    async def slow() -> Role | None:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return Role.VIEWER

    node = uuid.uuid4()
    results = await asyncio.gather(*(subject.permission("alice", node, slow) for _ in range(10)))

    assert all(role is Role.VIEWER for role in results)
    assert calls["n"] == 1


async def test_a_broken_cache_still_serves_the_right_answer() -> None:
    """Redis down means slower, never wrong."""
    cache = FakeCache()
    cache.fail = True
    cache.reachable = False

    assert await service(cache).permission("alice", uuid.uuid4(), _returns(Role.OWNER)) is (
        Role.OWNER
    )


# --- invalidation ----------------------------------------------------------


async def test_invalidating_a_subject_drops_their_decisions() -> None:
    cache = FakeCache()
    subject = service(cache)
    node = uuid.uuid4()
    await subject.permission("alice", node, _returns(Role.OWNER))
    await subject.permission("bob", node, _returns(Role.VIEWER))

    await subject.invalidate_permissions_for_subject("alice")

    load, calls = _counting(Role.OWNER)
    await subject.permission("alice", node, load)
    assert calls["n"] == 1, "alice was recomputed"

    other, other_calls = _counting(Role.VIEWER)
    await subject.permission("bob", node, other)
    assert other_calls["n"] == 0, "bob's decision survived"


async def test_a_move_drops_every_permission_decision() -> None:
    """Reparenting changes inherited access for everyone above and below."""
    cache = FakeCache()
    subject = service(cache)
    await subject.permission("alice", uuid.uuid4(), _returns(Role.OWNER))
    await subject.permission("bob", uuid.uuid4(), _returns(Role.VIEWER))

    await subject.invalidate_all_permissions()

    assert not [k for k in cache.store if ":perm:" in k]


async def test_a_mutation_drops_both_parent_listings() -> None:
    cache = FakeCache()
    subject = service(cache)
    old_parent, new_parent = uuid.uuid4(), uuid.uuid4()
    node = uuid.uuid4()
    cache.store[cache_key(1, Dataset.LISTING, old_parent, "alice", "", 50)] = ["stale"]
    cache.store[cache_key(1, Dataset.LISTING, new_parent, "alice", "", 50)] = ["stale"]

    await subject.on_node_mutated(node, old_parent=old_parent, new_parent=new_parent)

    assert not [k for k in cache.store if ":list:" in k]


async def test_a_failed_invalidation_fails_the_write() -> None:
    """A stale allow must never survive a reachable-but-refusing cache."""
    cache = FakeCache()
    cache.fail = True
    cache.reachable = True

    with pytest.raises(DependencyUnavailableError):
        await service(cache).invalidate_permissions_for_subject("alice")


async def test_a_known_down_cache_does_not_fail_the_write() -> None:
    """Nothing is being served from it, so there is no stale entry to fear."""
    cache = FakeCache()
    cache.fail = True
    cache.reachable = False

    await service(cache).invalidate_permissions_for_subject("alice")


# --- what is not cached ----------------------------------------------------


def test_audit_records_are_not_a_cacheable_dataset() -> None:
    """They must never be stale in an investigation."""
    assert "audit" not in {str(dataset) for dataset in Dataset}


def test_grant_listings_are_not_a_cacheable_dataset() -> None:
    assert "grants" not in {str(dataset) for dataset in Dataset}


async def test_no_cached_value_holds_content_or_key_material() -> None:
    """`caching/spec.md`: never plaintext, ciphertext, keys, or tokens."""
    cache = FakeCache()
    subject = service(cache)
    await subject.permission("alice", uuid.uuid4(), _returns(Role.OWNER))
    await subject.get_or_load(Dataset.QUOTA, (uuid.uuid4(),), _returns_value(1024))

    blob = repr(cache.store).lower()
    for forbidden in ("wrapped", "dek", "kek", "master_key", "bearer", "plaintext"):
        assert forbidden not in blob


def _returns_value(value: Any):
    async def load() -> Any:
        return value

    return load


# --- administration --------------------------------------------------------


async def test_purging_a_dataset_leaves_the_others() -> None:
    cache = FakeCache()
    subject = service(cache)
    await subject.permission("alice", uuid.uuid4(), _returns(Role.OWNER))
    await subject.get_or_load(Dataset.QUOTA, ("u",), _returns_value(7))

    removed = await subject.purge(Dataset.PERMISSION)

    assert removed == 1
    assert any(":quota:" in key for key in cache.store)


async def test_stats_report_counts_not_values() -> None:
    cache = FakeCache()
    subject = service(cache)
    await subject.permission("alice", uuid.uuid4(), _returns(Role.OWNER))

    stats = await subject.stats()

    assert stats["keys"] == 1
    assert "value" not in stats
