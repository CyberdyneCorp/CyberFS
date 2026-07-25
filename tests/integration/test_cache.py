"""The Redis adapter, and caching under real HTTP.

The revocation test is the point of the whole section: adding a cache must not
weaken the guarantee section 6 established, that a withdrawn grant is denied on
the very next request.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from http import HTTPStatus

import pytest
import redis.asyncio as aioredis
from fastapi.testclient import TestClient
from minio import Minio

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.adapters.outbound.cache.redis_cache import NullCache, RedisCache
from cyberfs.domain.cache import Dataset, cache_key
from cyberfs.domain.errors import CacheUnavailableError
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("CYBERFS_TEST_REDIS_URL", "redis://localhost:6380/0")
MINIO_ENDPOINT = os.environ.get("CYBERFS_TEST_MINIO_ENDPOINT", "localhost:9000")
ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
PAYLOAD = b"cached content" * 40

_redis_down: str | None = None
_minio_down: str | None = None


@pytest.fixture
async def cache() -> AsyncIterator[RedisCache]:
    global _redis_down
    if _redis_down is not None:
        pytest.skip(_redis_down)

    client = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    subject = RedisCache(
        client,
        operation_timeout=timedelta(seconds=2),
        circuit_trip_after=timedelta(seconds=10),
    )
    if not await subject.ping():
        _redis_down = f"no Redis at {REDIS_URL}"
        await client.aclose()
        pytest.skip(_redis_down)
    await client.flushdb()
    yield subject
    await client.aclose()


# --- the adapter -----------------------------------------------------------


async def test_a_value_round_trips(cache: RedisCache) -> None:
    key = cache_key(1, Dataset.METADATA, uuid.uuid4())
    await cache.set(key, {"name": "report"}, timedelta(seconds=60))
    assert await cache.get(key) == {"name": "report"}


async def test_a_missing_key_is_none(cache: RedisCache) -> None:
    assert await cache.get(cache_key(1, Dataset.METADATA, uuid.uuid4())) is None


async def test_every_write_carries_a_ttl(cache: RedisCache) -> None:
    """Nothing is stored indefinitely."""
    key = cache_key(1, Dataset.PERMISSION, "alice", uuid.uuid4())
    await cache.set(key, 30, timedelta(seconds=60))

    ttl = await cache._client.ttl(key)
    assert 0 < ttl <= 60


async def test_delete_removes_a_key(cache: RedisCache) -> None:
    key = cache_key(1, Dataset.METADATA, uuid.uuid4())
    await cache.set(key, "x", timedelta(seconds=60))
    await cache.delete(key)
    assert await cache.get(key) is None


async def test_delete_prefix_removes_a_whole_dataset(cache: RedisCache) -> None:
    node = uuid.uuid4()
    await cache.set(cache_key(1, Dataset.PERMISSION, "alice", node), 30, timedelta(seconds=60))
    await cache.set(cache_key(1, Dataset.PERMISSION, "bob", node), 10, timedelta(seconds=60))
    kept = cache_key(1, Dataset.METADATA, node)
    await cache.set(kept, "keep", timedelta(seconds=60))

    removed = await cache.delete_prefix("cyberfs:v1:perm:")

    assert removed == 2
    assert await cache.get(kept) == "keep"


async def test_delete_prefix_is_scoped_by_subject(cache: RedisCache) -> None:
    node = uuid.uuid4()
    await cache.set(cache_key(1, Dataset.PERMISSION, "alice", node), 30, timedelta(seconds=60))
    bobs = cache_key(1, Dataset.PERMISSION, "bob", node)
    await cache.set(bobs, 10, timedelta(seconds=60))

    await cache.delete_prefix("cyberfs:v1:perm:alice:")

    assert await cache.get(bobs) == 10


async def test_stats_report_counts_not_values(cache: RedisCache) -> None:
    await cache.set(cache_key(1, Dataset.METADATA, "x"), "secret-value", timedelta(seconds=60))
    stats = await cache.stats()

    assert stats["available"] is True
    assert stats["keys"] >= 1
    assert "secret-value" not in repr(stats)


async def test_an_unreachable_cache_reads_as_a_miss() -> None:
    """A broken cache must never raise on the read path."""
    client = aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1)
    broken = RedisCache(
        client,
        operation_timeout=timedelta(milliseconds=50),
        circuit_trip_after=timedelta(seconds=10),
    )
    assert await broken.get(cache_key(1, Dataset.METADATA, "x")) is None
    assert not await broken.ping()
    await client.aclose()


async def test_an_unreachable_cache_raises_on_invalidation() -> None:
    """Losing an invalidation is a correctness problem, so it surfaces."""
    client = aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1)
    broken = RedisCache(
        client,
        operation_timeout=timedelta(milliseconds=50),
        circuit_trip_after=timedelta(seconds=10),
    )
    with pytest.raises(CacheUnavailableError):
        await broken.delete(cache_key(1, Dataset.PERMISSION, "alice", "n"))
    await client.aclose()


async def test_the_null_cache_is_always_a_miss() -> None:
    null = NullCache()
    await null.set("k", "v", timedelta(seconds=60))
    assert await null.get("k") is None
    assert not null.available


# --- through the API -------------------------------------------------------


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    global _minio_down, _redis_down
    for reason in (_minio_down, _redis_down):
        if reason is not None:
            pytest.skip(reason)
    try:
        Minio(
            MINIO_ENDPOINT, access_key="cyberfs", secret_key="cyberfs-dev-secret", secure=False
        ).list_buckets()
    except Exception as exc:
        _minio_down = f"no MinIO at {MINIO_ENDPOINT}: {type(exc).__name__}"
        pytest.skip(_minio_down)

    settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        redis_url=REDIS_URL,
        minio_endpoint=MINIO_ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=f"cyberfs-cache-{uuid.uuid4().hex[:8]}",
        minio_secure=False,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    return str(client.get("/api/v1/nodes/root", headers=who).json()["id"])


def make_folder(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.post(f"/api/v1/nodes/{parent}/folders", json={"name": name}, headers=who)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def test_revocation_still_beats_the_cache(client: TestClient) -> None:
    """The guarantee section 6 established must survive adding a cache.

    Bob reads repeatedly first, so his permission decision is definitely
    cached, and the revocation still has to deny him on the next request.
    """
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "docs")
    client.put(
        f"/api/v1/nodes/{folder}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    for _ in range(3):
        assert client.get(f"/api/v1/nodes/{folder}", headers=BOB).status_code == HTTPStatus.OK

    client.delete(f"/api/v1/nodes/{folder}/grants/bob", headers=ALICE)

    assert client.get(f"/api/v1/nodes/{folder}", headers=BOB).status_code == HTTPStatus.NOT_FOUND


def test_a_narrowed_grant_takes_effect_at_once(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "docs")
    client.put(
        f"/api/v1/nodes/{folder}/grants",
        json={"recipient": "bob", "role": "editor"},
        headers=ALICE,
    )
    assert (
        client.post(
            f"/api/v1/nodes/{folder}/folders", json={"name": "bobs"}, headers=BOB
        ).status_code
        == HTTPStatus.CREATED
    )

    client.put(
        f"/api/v1/nodes/{folder}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )

    denied = client.post(f"/api/v1/nodes/{folder}/folders", json={"name": "again"}, headers=BOB)
    assert denied.status_code == HTTPStatus.FORBIDDEN


def test_a_new_child_appears_immediately(client: TestClient) -> None:
    """A cached listing must not hide a node created a moment later."""
    root = root_id(client, ALICE)
    assert client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()["items"] == []

    created = make_folder(client, ALICE, root, "fresh")

    listing = client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()
    assert [item["id"] for item in listing["items"]] == [created]


def test_a_rename_is_visible_immediately(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "before")
    assert client.get(f"/api/v1/nodes/{folder}", headers=ALICE).json()["name"] == "before"

    client.patch(f"/api/v1/nodes/{folder}/name", json={"name": "after"}, headers=ALICE)

    assert client.get(f"/api/v1/nodes/{folder}", headers=ALICE).json()["name"] == "after"


def test_a_move_updates_inherited_access_immediately(client: TestClient) -> None:
    root_id(client, BOB)
    alice_root = root_id(client, ALICE)
    shared = make_folder(client, ALICE, alice_root, "docs")
    private = make_folder(client, ALICE, alice_root, "private")
    item = make_folder(client, ALICE, shared, "item")
    client.put(
        f"/api/v1/nodes/{shared}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert client.get(f"/api/v1/nodes/{item}", headers=BOB).status_code == HTTPStatus.OK

    client.patch(f"/api/v1/nodes/{item}/parent", json={"parent_id": private}, headers=ALICE)

    assert client.get(f"/api/v1/nodes/{item}", headers=BOB).status_code == HTTPStatus.NOT_FOUND


def test_the_cache_is_reported_as_healthy(client: TestClient) -> None:
    body = client.get("/health/ready").json()
    entry = next(c for c in body["components"] if c["name"] == "cache")
    assert entry["criticality"] == "optional"
    assert entry["status"] == "up"


def test_redis_holds_no_content_or_key_material(client: TestClient) -> None:
    """`caching/spec.md`: never plaintext, ciphertext, keys, or tokens."""
    import redis as sync_redis

    root = root_id(client, ALICE)
    folder = make_folder(client, ALICE, root, "warm")
    client.put(f"/api/v1/nodes/{folder}/files/secret.bin", content=PAYLOAD, headers=ALICE)
    client.get(f"/api/v1/nodes/{folder}", headers=ALICE)

    connection = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        blob = " ".join(
            f"{key}={connection.get(key)}"
            for key in connection.scan_iter(match="cyberfs:*", count=500)
            if connection.type(key) == "string"
        ).lower()
    finally:
        connection.close()

    assert "cached content" not in blob
    for forbidden in ("wrapped", "dek", "kek", "master_key", "bearer"):
        assert forbidden not in blob
