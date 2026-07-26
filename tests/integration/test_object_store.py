"""The MinIO adapter against a real MinIO.

What only a real object store can tell us: that the async-to-blocking bridge
actually streams, that ranges are served by the server rather than by slicing
locally, and that a server-side copy works.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from minio import Minio

from cyberfs.adapters.outbound.objects.minio_store import MinioObjectStore
from cyberfs.domain.errors import NotFoundError

from .conftest import minio_access_key, minio_endpoint, minio_secret_key

pytestmark = pytest.mark.integration

ENDPOINT = minio_endpoint()
ACCESS_KEY = minio_access_key()
SECRET_KEY = minio_secret_key()
BUCKET = os.environ.get("CYBERFS_TEST_MINIO_BUCKET", "cyberfs-test")

_unreachable: str | None = None


async def stream(payload: bytes, chunk: int = 64 * 1024) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]


async def collect(source: AsyncIterator[bytes]) -> bytes:
    buffer = bytearray()
    async for piece in source:
        buffer.extend(piece)
    return bytes(buffer)


@pytest.fixture
async def store() -> MinioObjectStore:
    global _unreachable
    if _unreachable is not None:
        pytest.skip(_unreachable)

    subject = MinioObjectStore(
        Minio(ENDPOINT, access_key=ACCESS_KEY, secret_key=SECRET_KEY, secure=False),
        BUCKET,
    )
    try:
        await subject.ensure_bucket()
    except Exception as exc:
        _unreachable = f"no MinIO at {ENDPOINT}: {type(exc).__name__}"
        pytest.skip(_unreachable)
    return subject


def a_key() -> str:
    return f"{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}"


# --- round trip ------------------------------------------------------------


async def test_put_then_get_round_trips(store: MinioObjectStore) -> None:
    key, payload = a_key(), b"hello object store"
    assert await store.put(key, stream(payload)) == len(payload)
    assert await collect(store.get(key)) == payload


async def test_an_empty_object_round_trips(store: MinioObjectStore) -> None:
    key = a_key()
    assert await store.put(key, stream(b"")) == 0
    assert await collect(store.get(key)) == b""


async def test_a_large_object_round_trips(store: MinioObjectStore) -> None:
    """Larger than one multipart part, so the streaming bridge is exercised."""
    key = a_key()
    payload = bytes(range(256)) * 40_000  # ~10 MB, past the 5 MiB part size

    stored = await store.put(key, stream(payload))

    assert stored == len(payload)
    assert await collect(store.get(key)) == payload


async def test_binary_content_is_preserved_exactly(store: MinioObjectStore) -> None:
    key = a_key()
    payload = bytes(range(256))
    await store.put(key, stream(payload))
    assert await collect(store.get(key)) == payload


async def test_overwriting_replaces_the_content(store: MinioObjectStore) -> None:
    key = a_key()
    await store.put(key, stream(b"first"))
    await store.put(key, stream(b"second"))
    assert await collect(store.get(key)) == b"second"


# --- ranges ----------------------------------------------------------------


async def test_a_range_read_returns_only_that_range(store: MinioObjectStore) -> None:
    key = a_key()
    payload = bytes(range(256))
    await store.put(key, stream(payload))

    assert await collect(store.get(key, offset=10, length=20)) == payload[10:30]


async def test_a_range_to_the_end(store: MinioObjectStore) -> None:
    key = a_key()
    payload = bytes(range(256))
    await store.put(key, stream(payload))

    assert await collect(store.get(key, offset=200)) == payload[200:]


async def test_a_range_at_the_start(store: MinioObjectStore) -> None:
    key = a_key()
    payload = bytes(range(256))
    await store.put(key, stream(payload))

    assert await collect(store.get(key, offset=0, length=1)) == payload[:1]


# --- stat, delete, copy ----------------------------------------------------


async def test_stat_reports_the_size(store: MinioObjectStore) -> None:
    key = a_key()
    await store.put(key, stream(b"x" * 42))
    assert await store.stat(key) == 42


async def test_stat_of_a_missing_key_is_none(store: MinioObjectStore) -> None:
    assert await store.stat(a_key()) is None


async def test_delete_removes_the_object(store: MinioObjectStore) -> None:
    key = a_key()
    await store.put(key, stream(b"transient"))
    await store.delete(key)
    assert await store.stat(key) is None


async def test_deleting_a_missing_key_is_not_an_error(store: MinioObjectStore) -> None:
    await store.delete(a_key())


async def test_reading_a_missing_key_is_not_found(store: MinioObjectStore) -> None:
    with pytest.raises(NotFoundError):
        await collect(store.get(a_key()))


async def test_copy_duplicates_server_side(store: MinioObjectStore) -> None:
    """The bytes never transit the API, so ciphertext copies as-is."""
    source, target = a_key(), a_key()
    payload = b"copy me" * 1000
    await store.put(source, stream(payload))

    copied = await store.copy(source, target)

    assert copied == len(payload)
    assert await collect(store.get(target)) == payload


async def test_copying_a_missing_source_is_not_found(store: MinioObjectStore) -> None:
    with pytest.raises(NotFoundError):
        await store.copy(a_key(), a_key())


# --- listing ---------------------------------------------------------------


async def test_listing_finds_objects_under_a_prefix(store: MinioObjectStore) -> None:
    owner = uuid.uuid4()
    keys = {f"{owner}/{uuid.uuid4()}/{uuid.uuid4()}" for _ in range(3)}
    for key in keys:
        await store.put(key, stream(b"x"))

    found = {stored.key async for stored in store.list_keys(prefix=str(owner))}

    assert keys <= found


async def test_listing_reports_size_and_timestamp(store: MinioObjectStore) -> None:
    """The reaper needs `last_modified` to honour its grace period."""
    owner = uuid.uuid4()
    key = f"{owner}/{uuid.uuid4()}/{uuid.uuid4()}"
    await store.put(key, stream(b"x" * 17))

    entries = [stored async for stored in store.list_keys(prefix=str(owner))]

    assert len(entries) == 1
    assert entries[0].size == 17
    assert entries[0].last_modified is not None


async def test_ensure_bucket_is_idempotent(store: MinioObjectStore) -> None:
    await store.ensure_bucket()
    await store.ensure_bucket()
