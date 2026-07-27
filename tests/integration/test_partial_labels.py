"""Partial label updates against real Postgres and real Redis.

Everything here is something the unit suite cannot state. `FakeUnitOfWork` models
no unique constraint, no foreign key, no advisory lock and no isolation, so the
properties this change exists for -- two writers not losing each other,
`ON CONFLICT DO NOTHING` not raising, the per-node maximum holding against two
patches at once, two concurrent patches landing on two distinct revisions, the
reserved namespace surviving a replace that empties the table, and a no-op
invalidating nothing -- are only real once a database and a cache are enforcing
them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
import redis.asyncio as aioredis
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.adapters.outbound.db.unit_of_work import SqlUnitOfWork
from cyberfs.application.nodes import NodeService
from cyberfs.domain.cache import Dataset, cache_key
from cyberfs.domain.errors import ValidationError
from cyberfs.domain.nodes import (
    MAX_TAGS_PER_NODE,
    RESERVED_METADATA_PREFIX,
    Node,
    NodeKind,
)
from cyberfs.domain.users import User
from cyberfs.infrastructure.db import create_engine, create_session_factory
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings, minio_endpoint, redis_url

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
ENDPOINT = minio_endpoint()
#: The app's default, so the keys seeded below are the ones it invalidates.
SCHEMA_VERSION = 1
_unreachable: str | None = None


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    global _unreachable
    if _unreachable is not None:
        pytest.skip(_unreachable)
    from minio import Minio

    try:
        Minio(
            ENDPOINT, access_key="cyberfs", secret_key="cyberfs-dev-secret", secure=False
        ).list_buckets()
    except Exception as exc:  # pragma: no cover - environment probe
        _unreachable = f"no MinIO at {ENDPOINT}: {type(exc).__name__}"
        pytest.skip(_unreachable)

    settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        minio_endpoint=ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=f"cyberfs-patch-{uuid.uuid4().hex[:8]}",
        minio_secure=False,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


# --- helpers ---------------------------------------------------------------


def root_id(client: TestClient, who: dict[str, str]) -> str:
    response = client.get("/api/v1/nodes/root", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return str(response.json()["id"])


def folder(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.post(f"/api/v1/nodes/{parent}/folders", json={"name": name}, headers=who)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def patch_tags(
    client: TestClient,
    who: dict[str, str],
    node_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    headers: dict[str, str] | None = None,
):
    return client.patch(
        f"/api/v1/nodes/{node_id}/tags",
        json={"add": add or [], "remove": remove or []},
        headers={**who, **(headers or {})},
    )


def patch_metadata(
    client: TestClient,
    who: dict[str, str],
    node_id: str,
    *,
    pairs: dict[str, str] | None = None,
    remove: list[str] | None = None,
):
    return client.patch(
        f"/api/v1/nodes/{node_id}/metadata",
        json={
            "set": [{"key": k, "value": v} for k, v in (pairs or {}).items()],
            "remove": remove or [],
        },
        headers=who,
    )


def found(client: TestClient, who: dict[str, str], **params: object) -> set[str]:
    response = client.get("/api/v1/search", params=params, headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return {item["id"] for item in response.json()["items"]}


async def a_node(session_factory: async_sessionmaker[AsyncSession]) -> tuple[User, Node]:
    """One committed user and their root folder, addressable outside the API."""
    async with SqlUnitOfWork(session_factory) as uow:
        user_id = uuid.uuid4()
        root = Node(
            id=uuid.uuid4(),
            owner_id=user_id,
            kind=NodeKind.FOLDER,
            name="root",
            parent_id=None,
            created_at=NOW,
            updated_at=NOW,
        )
        user = User(
            id=user_id,
            subject=f"concurrent-{uuid.uuid4().hex[:8]}",
            root_folder_id=root.id,
            quota_bytes=10 * GB,
            created_at=NOW,
            updated_at=NOW,
        )
        await uow.users.add(user)
        await uow.flush()
        await uow.nodes.add(root)
        await uow.commit()
        return user, root


def a_service() -> NodeService:
    """No cache: the lock and the isolation level are what is under test here."""
    return NodeService(max_tree_depth=64, page_size_max=100)


def in_own_session[T](work: Callable[[SqlUnitOfWork], Awaitable[T]]) -> T:
    """Reach Postgres from a *synchronous* test, on an engine of its own.

    The `engine` fixture's asyncpg connections belong to the loop that created
    them, and a sync test has no loop running, so borrowing that engine here
    would use it from the wrong one. A private engine inside one `asyncio.run` is
    self-consistent; the schema and the truncation still come from the fixtures.
    """

    async def main() -> T:
        engine = create_engine(build_settings())
        try:
            async with SqlUnitOfWork(create_session_factory(engine)) as uow:
                result = await work(uow)
                await uow.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(main())


def in_own_redis[T](work: Callable[[aioredis.Redis], Awaitable[T]]) -> T:
    """The same shape for Redis, for the invalidation assertions."""

    async def main() -> T:
        redis = aioredis.from_url(redis_url(), encoding="utf-8", decode_responses=True)
        try:
            return await work(redis)
        finally:
            await redis.aclose()

    return asyncio.run(main())


# --- round trips through the API -------------------------------------------


def test_a_tag_patch_adds_and_removes_and_comes_back_on_the_node(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "reports")
    assert patch_tags(client, ALICE, node, add=["keep", "stale"]).status_code == HTTPStatus.OK

    response = patch_tags(client, ALICE, node, add=["FRESH"], remove=[" Stale "])

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["tags"] == ["fresh", "keep"]
    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["tags"] == ["fresh", "keep"]


def test_a_metadata_patch_leaves_keys_it_does_not_name_alone(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "annotated")
    assert (
        patch_metadata(client, ALICE, node, pairs={"keep": "1", "drop": "2"}).status_code
        == HTTPStatus.OK
    )

    response = patch_metadata(client, ALICE, node, pairs={"added": "3"}, remove=["drop"])

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["metadata"] == {"keep": "1", "added": "3"}


def test_a_patched_tag_is_findable_and_a_removed_one_is_not(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "searchable")
    patch_tags(client, ALICE, node, add=["indexed"])

    assert found(client, ALICE, tag="indexed") == {node}

    patch_tags(client, ALICE, node, remove=["indexed"])
    assert found(client, ALICE, tag="indexed") == set()


def test_a_replace_after_a_patch_wins_outright(client: TestClient) -> None:
    """A patch states a change; a replace states the whole collection."""
    node = folder(client, ALICE, root_id(client, ALICE), "replaced")
    patch_tags(client, ALICE, node, add=["patched"])

    replaced = client.put(f"/api/v1/nodes/{node}/tags", json={"tags": ["asserted"]}, headers=ALICE)

    assert replaced.status_code == HTTPStatus.OK, replaced.text
    assert replaced.json()["tags"] == ["asserted"]


def test_an_identical_patch_returns_the_same_etag(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "idempotent")
    first = patch_tags(client, ALICE, node, add=["once"])
    assert first.status_code == HTTPStatus.OK, first.text

    second = patch_tags(client, ALICE, node, add=["once"])

    assert second.status_code == HTTPStatus.OK, second.text
    assert second.headers["ETag"] == first.headers["ETag"], "a no-op must not move the validator"


def test_a_contradictory_patch_is_refused(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "contradiction")

    response = patch_tags(client, ALICE, node, add=["both"], remove=["BOTH"])

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


def test_an_empty_patch_is_refused(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "empty")

    assert patch_tags(client, ALICE, node).status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert patch_metadata(client, ALICE, node).status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_stale_precondition_is_refused_and_changes_nothing(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "guarded")
    stale = client.get(f"/api/v1/nodes/{node}", headers=ALICE).headers["ETag"]
    patch_tags(client, ALICE, node, add=["first"])

    response = patch_tags(client, ALICE, node, add=["second"], headers={"If-Match": stale})

    assert response.status_code == HTTPStatus.PRECONDITION_FAILED, response.text
    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["tags"] == ["first"]


def test_a_viewer_is_refused_end_to_end(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "shared")
    root_id(client, BOB)
    granted = client.put(
        f"/api/v1/nodes/{node}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED, granted.text

    assert patch_tags(client, BOB, node, add=["nope"]).status_code == HTTPStatus.FORBIDDEN
    assert patch_metadata(client, BOB, node, pairs={"k": "v"}).status_code == HTTPStatus.FORBIDDEN
    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["tags"] == []


def test_purging_still_removes_the_rows_a_patch_wrote(client: TestClient) -> None:
    """The `node_id` cascade, which the fake models with a dict and no key."""
    root = root_id(client, ALICE)
    node = folder(client, ALICE, root, "doomed")
    patch_tags(client, ALICE, node, add=["cascade"])
    patch_metadata(client, ALICE, node, pairs={"source": "sap"})

    assert client.delete(f"/api/v1/nodes/{node}", headers=ALICE).status_code == HTTPStatus.OK
    purged = client.post(f"/api/v1/nodes/{node}/purge", headers=ALICE)
    assert purged.status_code == HTTPStatus.OK, purged.text

    replacement = folder(client, ALICE, root, "replacement")
    patch_tags(client, ALICE, replacement, add=["cascade"])
    assert found(client, ALICE, tag="cascade") == {replacement}


# --- concurrency, which is the whole point ---------------------------------


async def test_two_concurrent_patches_of_different_tags_both_survive(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A replace would have had the second delete the first's row.

    Both go through the service, so both take the advisory lock and are ordered
    by it -- and the second still keeps the first's tag, because a patch only ever
    names its own rows.
    """
    user, node = await a_node(session_factory)
    svc = a_service()

    async def patch(tag: str) -> None:
        async with SqlUnitOfWork(session_factory) as uow:
            await svc.patch_tags(uow, user, node.id, add=[tag], now=LATER)
            await uow.commit()

    await asyncio.gather(patch("alpha"), patch("beta"))

    async with SqlUnitOfWork(session_factory) as uow:
        assert await uow.nodes.tags_for(node.id) == frozenset({"alpha", "beta"})


async def test_two_concurrent_patches_produce_two_distinct_revisions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The lock plus the bump under it.

    Without the lock both transactions read revision N and both persist N + 1,
    and two different label states share one validator.
    """
    user, node = await a_node(session_factory)
    svc = a_service()

    async def patch(tag: str) -> int:
        async with SqlUnitOfWork(session_factory) as uow:
            view, _ = await svc.patch_tags(uow, user, node.id, add=[tag], now=LATER)
            await uow.commit()
            return view.node.revision

    revisions = await asyncio.gather(patch("gamma"), patch("delta"))

    assert set(revisions) == {node.revision + 1, node.revision + 2}


async def test_two_concurrent_patches_cannot_jointly_exceed_the_maximum(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason the lock is here at all.

    Unserialized, both patches read `MAX - 1`, both compute a legal `MAX`, both
    insert, and the node ends holding `MAX + 1` -- a maximum a caller crosses by
    trying twice at once.
    """
    user, node = await a_node(session_factory)
    svc = a_service()
    async with SqlUnitOfWork(session_factory) as uow:
        await uow.nodes.add_tags(
            node.id, frozenset(f"tag-{i}" for i in range(MAX_TAGS_PER_NODE - 1))
        )
        await uow.commit()

    async def patch(tag: str) -> None:
        async with SqlUnitOfWork(session_factory) as uow:
            await svc.patch_tags(uow, user, node.id, add=[tag], now=LATER)
            await uow.commit()

    outcomes = await asyncio.gather(patch("late-a"), patch("late-b"), return_exceptions=True)

    refused = [o for o in outcomes if isinstance(o, ValidationError)]
    assert len(refused) == 1, f"expected exactly one refusal, got {outcomes}"
    async with SqlUnitOfWork(session_factory) as uow:
        assert len(await uow.nodes.tags_for(node.id)) == MAX_TAGS_PER_NODE


async def test_adding_a_tag_the_node_already_carries_does_not_violate_the_constraint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`ON CONFLICT DO NOTHING` against the unique constraint that makes it work."""
    _, node = await a_node(session_factory)

    async with SqlUnitOfWork(session_factory) as uow:
        await uow.nodes.add_tags(node.id, frozenset({"twice"}))
        await uow.commit()
    async with SqlUnitOfWork(session_factory) as uow:
        await uow.nodes.add_tags(node.id, frozenset({"twice", "another"}))
        await uow.commit()

    async with SqlUnitOfWork(session_factory) as uow:
        assert await uow.nodes.tags_for(node.id) == frozenset({"twice", "another"})


async def test_setting_a_key_that_exists_updates_it_in_place(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, node = await a_node(session_factory)

    async with SqlUnitOfWork(session_factory) as uow:
        await uow.nodes.set_metadata(node.id, {"source": "old", "other": "kept"})
        await uow.commit()
    async with SqlUnitOfWork(session_factory) as uow:
        await uow.nodes.set_metadata(node.id, {"source": "new"})
        await uow.commit()

    async with SqlUnitOfWork(session_factory) as uow:
        assert await uow.nodes.metadata_for(node.id) == {"source": "new", "other": "kept"}


# --- the reserved namespace ------------------------------------------------


async def test_a_reserved_pair_survives_a_replace_of_an_empty_collection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Written at the repository, as CyberFS itself would write it.

    The fake's replace is a dict assignment, so only real SQL can show that the
    delete leaves these rows in place.
    """
    _, node = await a_node(session_factory)
    reserved = f"{RESERVED_METADATA_PREFIX}origin"

    async with SqlUnitOfWork(session_factory) as uow:
        await uow.nodes.set_metadata(node.id, {reserved: "system", "mine": "x"})
        await uow.commit()
    async with SqlUnitOfWork(session_factory) as uow:
        await uow.nodes.replace_metadata(node.id, {})
        await uow.commit()

    async with SqlUnitOfWork(session_factory) as uow:
        assert await uow.nodes.metadata_for(node.id) == {reserved: "system"}


def test_a_reserved_pair_is_withheld_from_every_response(client: TestClient) -> None:
    """Seeded at the repository, then looked for everywhere a caller can look.

    The pair has to be invisible for `PUT`'s echo to stay valid: a caller that
    replaced the metadata it was handed would otherwise be refused for naming a
    reserved key it never chose.
    """
    node = uuid.UUID(folder(client, ALICE, root_id(client, ALICE), "seeded"))
    reserved = f"{RESERVED_METADATA_PREFIX}trusted"
    in_own_session(lambda uow: uow.nodes.set_metadata(node, {reserved: "yes"}))

    fetched = client.get(f"/api/v1/nodes/{node}", headers=ALICE)
    assert fetched.status_code == HTTPStatus.OK, fetched.text
    assert reserved not in fetched.json()["metadata"]

    replaced = client.put(f"/api/v1/nodes/{node}/metadata", json={"metadata": []}, headers=ALICE)
    assert replaced.status_code == HTTPStatus.OK, replaced.text
    assert replaced.json()["metadata"] == {}, "the reserved pair is not shown"

    patched = patch_metadata(client, ALICE, str(node), pairs={"mine": "x"})
    assert patched.status_code == HTTPStatus.OK, patched.text
    assert patched.json()["metadata"] == {"mine": "x"}

    # Still on the node, though: only the response is filtered.
    stored = in_own_session(lambda uow: uow.nodes.metadata_for(node))
    assert stored == {reserved: "yes", "mine": "x"}


def test_a_patch_may_not_name_a_reserved_key_as_a_removal(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "reserved")

    for key in (f"{RESERVED_METADATA_PREFIX}origin", "CyberFS.Origin"):
        response = patch_metadata(client, ALICE, node, remove=[key])
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


# --- the validator ---------------------------------------------------------


def test_the_etag_a_patch_returns_round_trips_through_a_read_and_a_precondition(
    client: TestClient,
) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "validated")

    first = patch_tags(client, ALICE, node, add=["one"])
    assert first.status_code == HTTPStatus.OK, first.text
    etag = first.headers["ETag"]

    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).headers["ETag"] == etag

    second = patch_tags(client, ALICE, node, add=["two"], headers={"If-Match": etag})
    assert second.status_code == HTTPStatus.OK, second.text
    assert second.json()["tags"] == ["one", "two"]


# --- the cache -------------------------------------------------------------


def test_a_no_op_patch_leaves_cached_entries_alone_and_a_real_one_drops_them(
    client: TestClient,
) -> None:
    """The one guarantee a fake cannot make: nothing was invalidated.

    The entries are seeded directly, because nothing populates the node and
    listing datasets on read yet -- what is under test is the invalidation, not
    the warming.
    """
    parent = root_id(client, ALICE)
    node = folder(client, ALICE, parent, "cached")
    patch_tags(client, ALICE, node, add=["settled"])

    view_key = cache_key(SCHEMA_VERSION, Dataset.METADATA, node)
    listing_key = cache_key(SCHEMA_VERSION, Dataset.LISTING, parent, "first-page")

    async def warm(redis: aioredis.Redis) -> None:
        await redis.set(view_key, '{"warm": true}', ex=300)
        await redis.set(listing_key, '{"warm": true}', ex=300)

    async def read(redis: aioredis.Redis) -> tuple[object, object]:
        return await redis.get(view_key), await redis.get(listing_key)

    def patch_and_read(add: str) -> tuple[object, object]:
        in_own_redis(warm)
        response = patch_tags(client, ALICE, node, add=[add])
        assert response.status_code == HTTPStatus.OK, response.text
        return in_own_redis(read)

    # Adding a tag the node already carries changes nothing, so nothing is stale.
    survived = patch_and_read("settled")
    assert all(entry is not None for entry in survived), "a no-op invalidated a cached view"

    dropped = patch_and_read("actually-new")
    assert all(entry is None for entry in dropped), "a real patch left a stale cached view"
