"""Tags, metadata, search, and the content digest, end to end.

What this adds over the unit tests is everything a fake cannot show: that the
search SQL actually narrows the way the filters promise, that the access scope
holds against real joins, and that the foreign-key cascade removes labels when a
node is purged.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings, minio_endpoint

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer dev:root:admin"}
ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
OCTET = "application/octet-stream"
ENDPOINT = minio_endpoint()
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
        minio_bucket=f"cyberfs-labels-{uuid.uuid4().hex[:8]}",
        minio_secure=False,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    response = client.get("/api/v1/nodes/root", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return str(response.json()["id"])


def folder(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.post(f"/api/v1/nodes/{parent}/folders", json={"name": name}, headers=who)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def upload(client: TestClient, who: dict[str, str], parent: str, name: str, body: bytes) -> str:
    response = client.put(
        f"/api/v1/nodes/{parent}/files/{name}",
        content=body,
        headers={**who, "Content-Type": OCTET},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def tag(client: TestClient, who: dict[str, str], node_id: str, *tags: str) -> dict:
    response = client.put(f"/api/v1/nodes/{node_id}/tags", json={"tags": list(tags)}, headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return dict(response.json())


def annotate(client: TestClient, who: dict[str, str], node_id: str, **pairs: str) -> dict:
    response = client.put(
        f"/api/v1/nodes/{node_id}/metadata",
        json={"metadata": [{"key": k, "value": v} for k, v in pairs.items()]},
        headers=who,
    )
    assert response.status_code == HTTPStatus.OK, response.text
    return dict(response.json())


def found(client: TestClient, who: dict[str, str], **params: object) -> set[str]:
    response = client.get("/api/v1/search", params=params, headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return {item["id"] for item in response.json()["items"]}


# --- round trips -----------------------------------------------------------


def test_tags_round_trip_and_appear_on_the_node(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "reports")

    body = tag(client, ALICE, node, "Q3", "urgent")

    assert body["tags"] == ["q3", "urgent"], "stored normalized and sorted"
    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["tags"] == ["q3", "urgent"]


def test_metadata_round_trips_and_appears_on_the_node(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "reports")

    body = annotate(client, ALICE, node, source="sap", batch="42")

    assert body["metadata"] == {"source": "sap", "batch": "42"}
    fetched = client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()
    assert fetched["metadata"] == {"source": "sap", "batch": "42"}


def test_replacing_with_an_empty_list_clears(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "reports")
    tag(client, ALICE, node, "temporary")

    assert tag(client, ALICE, node)["tags"] == []


def test_a_duplicate_metadata_key_is_refused(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "reports")

    response = client.put(
        f"/api/v1/nodes/{node}/metadata",
        json={"metadata": [{"key": "a", "value": "1"}, {"key": "a", "value": "2"}]},
        headers=ALICE,
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


def test_a_stale_if_match_is_refused(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "reports")
    stale = client.get(f"/api/v1/nodes/{node}", headers=ALICE).headers["ETag"]
    tag(client, ALICE, node, "first")

    response = client.put(
        f"/api/v1/nodes/{node}/tags",
        json={"tags": ["second"]},
        headers={**ALICE, "If-Match": stale},
    )

    assert response.status_code == HTTPStatus.PRECONDITION_FAILED, response.text
    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["tags"] == ["first"]


# --- search ----------------------------------------------------------------


def test_search_by_one_tag(client: TestClient) -> None:
    root = root_id(client, ALICE)
    match = folder(client, ALICE, root, "tagged")
    other = folder(client, ALICE, root, "untagged")
    tag(client, ALICE, match, "urgent")

    results = found(client, ALICE, tag="urgent")

    assert results == {match}
    assert other not in results


def test_two_tags_return_only_nodes_carrying_both(client: TestClient) -> None:
    root = root_id(client, ALICE)
    both = folder(client, ALICE, root, "both")
    one = folder(client, ALICE, root, "one")
    tag(client, ALICE, both, "a", "b")
    tag(client, ALICE, one, "a")

    assert found(client, ALICE, tag=["a", "b"]) == {both}


def test_search_by_metadata_key_and_by_key_with_value(client: TestClient) -> None:
    root = root_id(client, ALICE)
    node = folder(client, ALICE, root, "annotated")
    annotate(client, ALICE, node, source="sap")

    assert found(client, ALICE, key="source") == {node}
    assert found(client, ALICE, key="source", value="sap") == {node}
    assert found(client, ALICE, key="source", value="other") == set()


def test_a_name_and_a_tag_narrow_together(client: TestClient) -> None:
    root = root_id(client, ALICE)
    match = folder(client, ALICE, root, "report-keep")
    other = folder(client, ALICE, root, "report-drop")
    tag(client, ALICE, match, "keep")
    tag(client, ALICE, other, "drop")

    assert found(client, ALICE, q="report") == {match, other}
    assert found(client, ALICE, q="report", tag="keep") == {match}


def test_a_search_with_no_filter_is_refused(client: TestClient) -> None:
    root_id(client, ALICE)
    response = client.get("/api/v1/search", headers=ALICE)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


def test_another_users_tagged_node_never_appears(client: TestClient) -> None:
    hidden = folder(client, ALICE, root_id(client, ALICE), "alices-private")
    tag(client, ALICE, hidden, "shared-word")
    root_id(client, BOB)

    assert found(client, BOB, tag="shared-word") == set()


def test_an_active_grant_makes_a_tagged_node_findable(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "shared-folder")
    tag(client, ALICE, node, "collab")
    root_id(client, BOB)
    granted = client.put(
        f"/api/v1/nodes/{node}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED, granted.text

    assert found(client, BOB, tag="collab") == {node}


def test_a_pending_grant_does_not_make_a_tagged_node_findable(
    engine: object, session_factory: object
) -> None:
    """A pending share confers no access, and search must honour that.

    Built with the async-rewrap threshold at zero so any share of an encrypted
    subtree is deferred: the grant exists but is pending until the worker has
    rewrapped every key, and until then the recipient must not be able to find
    the node by tag any more than they could read it.
    """
    settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        minio_endpoint=ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=f"cyberfs-pending-{uuid.uuid4().hex[:8]}",
        minio_secure=False,
        async_rewrap_threshold_nodes=0,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        root = root_id(client, ALICE)
        node = folder(client, ALICE, root, "deferred")
        # An encrypted child is what makes the rewrap deferrable.
        encrypted = client.put(
            f"/api/v1/nodes/{node}/files/sealed.bin",
            content=os.urandom(512),
            params={"encrypted": "true"},
            headers={**ALICE, "Content-Type": OCTET},
        )
        assert encrypted.status_code == HTTPStatus.CREATED, encrypted.text
        tag(client, ALICE, node, "deferred-label")
        root_id(client, BOB)

        granted = client.put(
            f"/api/v1/nodes/{node}/grants",
            json={"recipient": "bob", "role": "viewer"},
            headers=ALICE,
        )
        assert granted.status_code == HTTPStatus.CREATED, granted.text

        assert found(client, BOB, tag="deferred-label") == set()


def test_a_trashed_node_is_not_found(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "doomed")
    tag(client, ALICE, node, "gone")
    assert client.delete(f"/api/v1/nodes/{node}", headers=ALICE).status_code == HTTPStatus.OK

    assert found(client, ALICE, tag="gone") == set()


def test_results_are_bounded(client: TestClient) -> None:
    root = root_id(client, ALICE)
    for i in range(5):
        tag(client, ALICE, folder(client, ALICE, root, f"many-{i}"), "bulk")

    response = client.get("/api/v1/search", params={"tag": "bulk", "limit": 2}, headers=ALICE)
    assert response.status_code == HTTPStatus.OK, response.text
    assert len(response.json()["items"]) == 2


# --- digest ----------------------------------------------------------------


@pytest.mark.parametrize("encrypted", [False, True])
def test_the_digest_matches_the_uploaded_bytes(client: TestClient, encrypted: bool) -> None:
    """Encrypted or not: the digest is of the plaintext, so it is the same either way."""
    root = root_id(client, ALICE)
    body = os.urandom(4096)
    expected = hashlib.sha256(body).hexdigest()
    response = client.put(
        f"/api/v1/nodes/{root}/files/hashed-{encrypted}.bin",
        content=body,
        params={"encrypted": str(encrypted).lower()},
        headers={**ALICE, "Content-Type": OCTET},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    node = response.json()["id"]

    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["digest"] == expected
    versions = client.get(f"/api/v1/nodes/{node}/versions", headers=ALICE).json()["items"]
    assert versions[0]["digest"] == expected


def test_a_folder_has_no_digest(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "no-content")
    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["digest"] is None


def test_no_digest_reaches_the_admin_surface(client: TestClient) -> None:
    """A plaintext digest would let an administrator confirm which user holds a
    known file, which encryption is meant to prevent."""
    root = root_id(client, ALICE)
    upload(client, ALICE, root, "secret.bin", os.urandom(1024))

    for path in ("/api/v1/admin/overview", "/api/v1/admin/users", "/api/v1/admin/audit"):
        response = client.get(path, headers=ADMIN)
        assert response.status_code == HTTPStatus.OK, response.text
        assert "digest" not in response.text


# --- lifecycle -------------------------------------------------------------


def test_purging_removes_tags_and_metadata(client: TestClient) -> None:
    """The foreign-key cascade, which the fake cannot model."""
    root = root_id(client, ALICE)
    node = folder(client, ALICE, root, "labelled")
    tag(client, ALICE, node, "doomed")
    annotate(client, ALICE, node, source="sap")

    assert client.delete(f"/api/v1/nodes/{node}", headers=ALICE).status_code == HTTPStatus.OK
    purged = client.post(f"/api/v1/nodes/{node}/purge", headers=ALICE)
    assert purged.status_code == HTTPStatus.OK, purged.text

    # The rows are gone with the node: a new node reusing the tag finds only itself.
    replacement = folder(client, ALICE, root, "replacement")
    tag(client, ALICE, replacement, "doomed")
    assert found(client, ALICE, tag="doomed") == {replacement}


def test_a_copy_does_not_inherit_labels(client: TestClient) -> None:
    root = root_id(client, ALICE)
    source = folder(client, ALICE, root, "source")
    destination = folder(client, ALICE, root, "destination")
    tag(client, ALICE, source, "private-label")

    copied = client.post(
        f"/api/v1/nodes/{source}/copy", json={"parent_id": destination}, headers=ALICE
    )
    assert copied.status_code == HTTPStatus.CREATED, copied.text

    assert copied.json()["tags"] == []
    assert found(client, ALICE, tag="private-label") == {source}
