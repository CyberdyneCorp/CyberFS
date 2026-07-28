"""The trash surface, end to end.

Real HTTP, real Postgres, real MinIO. What this adds over the unit tests is
everything a fake cannot show: the recursive aggregate behind an entry's totals,
the partial index the listing depends on, the FK cascades a purge relies on, and
that emptying the trash leaves no object in the bucket that no row references.

`FakeUnitOfWork` models no foreign keys, so none of that is provable against it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from minio import Minio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cyberfs.adapters.inbound.api.app import create_app

# Private on purpose: the point of the plan test is to EXPLAIN the predicate the
# repository actually issues, so it reaches for the repository's own helper rather
# than retyping a query that could drift away from it.
from cyberfs.adapters.outbound.db.unit_of_work import SqlUnitOfWork
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings, minio_endpoint

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
PAYLOAD = b"trash-me-for-real" * 16
OCTET = "application/octet-stream"

#: Rows that ONLY the FK cascade removes -- nothing in `application/purge.py`
#: deletes them, so if the cascade were dropped they would survive a purge.
CASCADE_ONLY_TABLES = ("node_tags", "node_metadata", "public_links")

#: Rows `_strip` deletes explicitly. Asserted too, because "no row references a
#: purged node" must hold however it is achieved -- but they are not evidence
#: about the cascade, which is why they are named apart.
EXPLICITLY_STRIPPED_TABLES = ("grants", "wrapped_data_keys", "file_versions")

ENDPOINT = minio_endpoint()
_unreachable: str | None = None


@pytest.fixture
def bucket(engine: AsyncEngine, session_factory: object) -> Iterator[str]:
    """A bucket of its own per test, so an orphan check sees only this test."""
    global _unreachable
    if _unreachable is not None:
        pytest.skip(_unreachable)
    try:
        _minio().list_buckets()
    except Exception as exc:  # pragma: no cover - environment probe
        _unreachable = f"no MinIO at {ENDPOINT}: {type(exc).__name__}"
        pytest.skip(_unreachable)
    yield f"cyberfs-trash-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def client(bucket: str) -> Iterator[TestClient]:
    settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        minio_endpoint=ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=bucket,
        minio_secure=False,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _minio() -> Minio:
    return Minio(ENDPOINT, access_key="cyberfs", secret_key="cyberfs-dev-secret", secure=False)


def stored_keys(bucket: str) -> set[str]:
    return {entry.object_name or "" for entry in _minio().list_objects(bucket, recursive=True)}


def root_id(client: TestClient, who: dict[str, str]) -> str:
    response = client.get("/api/v1/nodes/root", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return str(response.json()["id"])


def make_folder(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.post(f"/api/v1/nodes/{parent}/folders", headers=who, json={"name": name})
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def upload(
    client: TestClient,
    who: dict[str, str],
    parent: str,
    name: str,
    body: bytes = PAYLOAD,
    *,
    encrypted: bool | None = None,
) -> str:
    query = "" if encrypted is None else f"?encrypted={str(encrypted).lower()}"
    response = client.put(
        f"/api/v1/nodes/{parent}/files/{name}{query}",
        content=body,
        headers={**who, "Content-Type": OCTET},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def trash(client: TestClient, who: dict[str, str], **params: object) -> dict:
    response = client.get("/api/v1/trash", headers=who, params=params)
    assert response.status_code == HTTPStatus.OK, response.text
    return dict(response.json())


def delete(client: TestClient, who: dict[str, str], node_id: str) -> None:
    response = client.delete(f"/api/v1/nodes/{node_id}", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text


def grant(client: TestClient, node_id: str, subject: str, role: str) -> None:
    root_id(client, BOB)  # provision the recipient before granting to them
    response = client.put(
        f"/api/v1/nodes/{node_id}/grants",
        headers=ALICE,
        json={"recipient": subject, "role": role},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text


# --- the loop the API could not previously complete ------------------------


def test_a_deleted_file_is_found_in_the_trash_and_restored_from_it(client: TestClient) -> None:
    """Delete, list, restore by the identifier the listing supplied, download.

    The whole point of the change: nothing here remembers an id from before the
    delete except the assertion, and the restore is driven by what the trash
    returned.
    """
    root = root_id(client, ALICE)
    folder = make_folder(client, ALICE, root, "reports")
    node_id = upload(client, ALICE, folder, "q3.bin")
    delete(client, ALICE, node_id)

    listing = trash(client, ALICE)
    assert [item["id"] for item in listing["items"]] == [node_id]
    entry = listing["items"][0]
    assert entry["path"] == "/reports/q3.bin"
    assert entry["size_bytes"] == len(PAYLOAD)
    assert entry["node_count"] == 1
    assert entry["purge_after"] > entry["deleted_at"]

    restored = client.post(f"/api/v1/nodes/{entry['id']}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK, restored.text
    content = client.get(f"/api/v1/nodes/{node_id}/content", headers=ALICE)
    assert content.content == PAYLOAD
    assert trash(client, ALICE)["items"] == []


def test_no_digest_or_object_key_is_reported(client: TestClient) -> None:
    """An entry is metadata about a node, not a handle on its content."""
    root = root_id(client, ALICE)
    node_id = upload(client, ALICE, root, "opaque.bin")
    delete(client, ALICE, node_id)

    entry = trash(client, ALICE)["items"][0]

    assert "digest" not in entry
    assert not any("key" in field for field in entry)


# --- collapse and totals --------------------------------------------------


def test_a_deleted_folder_is_one_entry_with_the_subtrees_totals(client: TestClient) -> None:
    """The totals come from the recursive aggregate, not from the fake."""
    root = root_id(client, ALICE)
    outer = make_folder(client, ALICE, root, "outer")
    inner = make_folder(client, ALICE, outer, "inner")
    upload(client, ALICE, outer, "a.bin")
    upload(client, ALICE, inner, "b.bin")
    delete(client, ALICE, outer)

    listing = trash(client, ALICE)

    assert [item["id"] for item in listing["items"]] == [outer]
    assert listing["items"][0]["size_bytes"] == 2 * len(PAYLOAD)
    assert listing["items"][0]["node_count"] == 4


def test_pages_stay_full_while_entries_remain(client: TestClient) -> None:
    root = root_id(client, ALICE)
    for index in range(5):
        node_id = upload(client, ALICE, root, f"f{index}.bin")
        delete(client, ALICE, node_id)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = trash(client, ALICE, **params)
        pages += 1
        if page["next_cursor"] is not None:
            assert len(page["items"]) == 2, "a short page while entries remained"
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert len(seen) == len(set(seen)) == 5


# --- isolation ------------------------------------------------------------


def test_bobs_trash_never_shows_alices_nodes(client: TestClient) -> None:
    alice_node = upload(client, ALICE, root_id(client, ALICE), "alice.bin")
    # A grant that existed at delete time confers nothing afterwards.
    grant(client, alice_node, "bob", "editor")
    delete(client, ALICE, alice_node)

    assert trash(client, BOB)["items"] == []
    assert [item["id"] for item in trash(client, ALICE)["items"]] == [alice_node]


# --- cascading restore ----------------------------------------------------


def test_restoring_a_folder_makes_every_descendant_reachable_again(client: TestClient) -> None:
    root = root_id(client, ALICE)
    outer = make_folder(client, ALICE, root, "outer")
    inner = make_folder(client, ALICE, outer, "inner")
    leaf = upload(client, ALICE, inner, "leaf.bin")
    delete(client, ALICE, outer)

    entry = trash(client, ALICE)["items"][0]
    assert client.post(f"/api/v1/nodes/{entry['id']}/restore", headers=ALICE).status_code == (
        HTTPStatus.OK
    )

    children = client.get(f"/api/v1/nodes/{inner}/children", headers=ALICE)
    assert children.status_code == HTTPStatus.OK, children.text
    assert [item["id"] for item in children.json()["items"]] == [leaf]
    assert client.get(f"/api/v1/nodes/{leaf}/content", headers=ALICE).content == PAYLOAD


def test_a_separately_deleted_child_stays_trashed_then_restores_alone(client: TestClient) -> None:
    root = root_id(client, ALICE)
    folder = make_folder(client, ALICE, root, "folder")
    kept = upload(client, ALICE, folder, "kept.bin")
    dropped = upload(client, ALICE, folder, "dropped.bin")
    delete(client, ALICE, dropped)
    delete(client, ALICE, folder)

    assert [item["id"] for item in trash(client, ALICE)["items"]] == [folder]
    restored = client.post(f"/api/v1/nodes/{folder}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK, restored.text

    assert client.get(f"/api/v1/nodes/{kept}/content", headers=ALICE).content == PAYLOAD
    assert [item["id"] for item in trash(client, ALICE)["items"]] == [dropped]

    assert client.post(f"/api/v1/nodes/{dropped}/restore", headers=ALICE).status_code == (
        HTTPStatus.OK
    )
    assert client.get(f"/api/v1/nodes/{dropped}/content", headers=ALICE).content == PAYLOAD


async def test_quota_after_delete_then_restore_matches_a_recomputation(
    client: TestClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The drift the cascading restore fixes, against real rows.

    The counters are an accelerator; `recompute` is the truth the reconciliation
    job compares them with. A restore that cleared one row while releasing a whole
    subtree's bytes would show up here as a live/trashed disagreement.
    """
    root = root_id(client, ALICE)
    folder = make_folder(client, ALICE, root, "folder")
    upload(client, ALICE, folder, "a.bin")
    upload(client, ALICE, folder, "b.bin")
    delete(client, ALICE, folder)
    restored = client.post(f"/api/v1/nodes/{folder}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK, restored.text

    async with SqlUnitOfWork(session_factory) as uow:
        alice = await uow.users.get_by_subject("alice")
        assert alice is not None
        usage = await uow.quotas.get(alice.id)
        truth = await uow.quotas.recompute(alice.id)

    assert usage is not None
    assert usage.live_bytes == truth.live_bytes == 2 * len(PAYLOAD)
    assert usage.trashed_bytes == truth.trashed_bytes == 0


# --- emptying the trash ---------------------------------------------------


def test_emptying_the_trash_frees_the_space_and_leaves_no_orphan(
    client: TestClient, bucket: str
) -> None:
    root = root_id(client, ALICE)
    folder = make_folder(client, ALICE, root, "outer")
    upload(client, ALICE, folder, "a.bin")
    upload(client, ALICE, folder, "b.bin")
    kept = upload(client, ALICE, root, "kept.bin")
    delete(client, ALICE, folder)

    response = client.post("/api/v1/trash/purge", headers=ALICE, json={"expected_entries": 1})

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["entries_purged"] == 1
    assert body["entries_remaining"] == 0
    assert body["nodes_destroyed"] == 3
    assert body["objects_deleted"] == 2
    assert body["bytes_reclaimed"] == 2 * len(PAYLOAD)
    assert trash(client, ALICE)["items"] == []
    # Exactly one object survives: the live file nobody deleted.
    assert len(stored_keys(bucket)) == 1
    assert client.get(f"/api/v1/nodes/{kept}/content", headers=ALICE).content == PAYLOAD


def test_a_stale_count_is_refused_and_the_trash_survives(client: TestClient, bucket: str) -> None:
    root = root_id(client, ALICE)
    for index in range(2):
        delete(client, ALICE, upload(client, ALICE, root, f"e{index}.bin"))
    before = stored_keys(bucket)

    response = client.post("/api/v1/trash/purge", headers=ALICE, json={"expected_entries": 1})

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert len(trash(client, ALICE)["items"]) == 2
    assert stored_keys(bucket) == before


async def test_emptying_removes_the_rows_only_a_cascade_reaches(
    client: TestClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A shared, encrypted, multi-version, linked file leaves nothing behind.

    The spec requires emptying to destroy "metadata, every version's stored
    object, wrapped data keys, grants, and public links". `application/purge.py`
    deletes grants, wrapped keys and version rows explicitly; tags, metadata and
    **public links** go only because `nodes.id` cascades, and nothing but a real
    database can show that. The public link is the one the docstring of `purge_one`
    asserts and nothing proved.
    """
    root = root_id(client, ALICE)
    node_id = upload(client, ALICE, root, "shared.bin", encrypted=True)
    # A second version, not a second file: re-PUTting the same name under
    # `/files/{name}` is a create and conflicts, which is what CI reported.
    versioned = client.put(
        f"/api/v1/nodes/{node_id}/content",
        content=PAYLOAD + b"v2",
        headers={**ALICE, "Content-Type": OCTET},
    )
    assert versioned.status_code == HTTPStatus.OK, versioned.text
    tagged = client.put(
        f"/api/v1/nodes/{node_id}/tags", headers=ALICE, json={"tags": ["quarterly"]}
    )
    assert tagged.status_code == HTTPStatus.OK, tagged.text
    labelled = client.put(
        f"/api/v1/nodes/{node_id}/metadata",
        headers=ALICE,
        json={"metadata": [{"key": "team", "value": "finance"}]},
    )
    assert labelled.status_code == HTTPStatus.OK, labelled.text
    grant(client, node_id, "bob", "viewer")
    linked = client.post(f"/api/v1/nodes/{node_id}/links", headers=ALICE, json={})
    assert linked.status_code == HTTPStatus.CREATED, linked.text
    delete(client, ALICE, node_id)

    async with SqlUnitOfWork(session_factory) as uow:
        # Prove there is something for the cascade to reach, or the assertions
        # below would pass against a file that never had any of these rows.
        for table in CASCADE_ONLY_TABLES:
            query = f"SELECT count(*) FROM {table} WHERE node_id = CAST(:node_id AS uuid)"  # noqa: S608
            assert await uow.session.scalar(text(query), {"node_id": node_id})

    purged = client.post("/api/v1/trash/purge", headers=ALICE, json={"expected_entries": 1})
    assert purged.status_code == HTTPStatus.OK, purged.text

    async with SqlUnitOfWork(session_factory) as uow:
        for table in (*CASCADE_ONLY_TABLES, *EXPLICITLY_STRIPPED_TABLES):
            # `table` comes from this module's own tuple, never from a request.
            query = f"SELECT count(*) FROM {table} WHERE node_id = CAST(:node_id AS uuid)"  # noqa: S608
            remaining = await uow.session.scalar(text(query), {"node_id": node_id})
            assert remaining == 0, f"{table} kept a row for a purged node"
        node_rows = await uow.session.scalar(
            text("SELECT count(*) FROM nodes WHERE id = CAST(:node_id AS uuid)"),
            {"node_id": node_id},
        )
    assert node_rows == 0


# --- the index and the invariants this must not loosen --------------------


async def test_the_trash_listing_index_exists_and_is_partial(engine: AsyncEngine) -> None:
    """Without it, one user's trash means scanning every trashed row there is."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'nodes' AND indexname = 'ix_nodes_owner_trash'"
            )
        )
        definition = result.scalar_one()
    assert "owner_id" in definition
    assert "deleted_at" in definition
    assert "deleted_at IS NOT NULL" in definition


# The listing's *plan* is deliberately not asserted here. Task 6.13 says why, and
# CI proved it: on a freshly migrated database the planner correctly prefers a
# sequential scan over `ix_nodes_owner_trash`, so an EXPLAIN assertion either
# fails on a correct plan or passes for the wrong reason. Whether the index earns
# its keep is measured on a seeded corpus under task 8.4, not in CI.


def test_a_trashed_node_stays_out_of_listings_and_search(client: TestClient) -> None:
    root = root_id(client, ALICE)
    folder = make_folder(client, ALICE, root, "holder")
    node_id = upload(client, ALICE, folder, "findable.bin")
    delete(client, ALICE, node_id)

    children = client.get(f"/api/v1/nodes/{folder}/children", headers=ALICE)
    assert [item["id"] for item in children.json()["items"]] == []
    results = client.get("/api/v1/search", headers=ALICE, params={"q": "findable"})
    assert results.json()["items"] == []
    assert client.get(f"/api/v1/nodes/{node_id}", headers=ALICE).status_code == HTTPStatus.NOT_FOUND
