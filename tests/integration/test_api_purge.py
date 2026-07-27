"""The purge endpoint, end to end.

Real HTTP, real Postgres, real object store. What this adds over the unit tests
is the parts a fake cannot show: the status codes the API actually returns, and
that the database cascade does not undo the explicit object deletion.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer dev:root:admin"}
ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
PAYLOAD = b"purge-me-for-real" * 16
OCTET = "application/octet-stream"


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    settings = build_settings(auth_dev_mode=True, environment=Environment.TEST)
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    response = client.get("/api/v1/nodes/root", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return str(response.json()["id"])


def upload(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.put(
        f"/api/v1/nodes/{parent}/files/{name}",
        content=PAYLOAD,
        headers={**who, "Content-Type": OCTET},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def usage(client: TestClient, who: dict[str, str]) -> dict[str, int]:
    """The owner's byte totals, read through the admin surface.

    Queried as an administrator, not as `who`: `/api/v1/admin/users` requires
    admin rights, so asking as the owner is a 403.

    These are aggregates over the `nodes` rows (`SqlAdminQueries._node_counts`),
    NOT the `quota_usage` counters -- no endpoint exposes those. So a test built
    on this proves the rows were destroyed, which is what purge is for; it does
    not prove the counters moved with them. That belongs to the unit tests, which
    can compare against `quotas.recompute`.
    """
    listing = client.get("/api/v1/admin/users", headers=ADMIN)
    assert listing.status_code == HTTPStatus.OK, listing.text
    subject = who["Authorization"].removeprefix("Bearer dev:")
    for item in listing.json()["items"]:
        if item["subject"] == subject:
            return {k: item[k] for k in ("used_bytes", "live_bytes", "trashed_bytes")}
    raise AssertionError(f"no usage row for {subject}")


# --- guards ----------------------------------------------------------------


def test_a_live_node_cannot_be_purged(client: TestClient) -> None:
    node_id = upload(client, ALICE, root_id(client, ALICE), "live.bin")

    response = client.post(f"/api/v1/nodes/{node_id}/purge", headers=ALICE)

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    # Still there, and still downloadable.
    assert client.get(f"/api/v1/nodes/{node_id}/content", headers=ALICE).content == PAYLOAD


def test_an_unknown_node_is_not_found(client: TestClient) -> None:
    response = client.post(f"/api/v1/nodes/{uuid.uuid4()}/purge", headers=ALICE)
    assert response.status_code == HTTPStatus.NOT_FOUND, response.text


def test_purging_twice_is_not_found_the_second_time(client: TestClient) -> None:
    node_id = upload(client, ALICE, root_id(client, ALICE), "once.bin")
    client.delete(f"/api/v1/nodes/{node_id}", headers=ALICE)

    assert client.post(f"/api/v1/nodes/{node_id}/purge", headers=ALICE).status_code == HTTPStatus.OK
    second = client.post(f"/api/v1/nodes/{node_id}/purge", headers=ALICE)
    assert second.status_code == HTTPStatus.NOT_FOUND, second.text


def test_another_user_cannot_purge(client: TestClient) -> None:
    node_id = upload(client, ALICE, root_id(client, ALICE), "mine.bin")
    client.delete(f"/api/v1/nodes/{node_id}", headers=ALICE)
    root_id(client, BOB)  # provision Bob

    response = client.post(f"/api/v1/nodes/{node_id}/purge", headers=BOB)

    # 404 rather than 403: a caller with no role is not told the node exists.
    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert client.get(f"/api/v1/nodes/{node_id}", headers=ALICE).status_code in (
        HTTPStatus.OK,
        HTTPStatus.NOT_FOUND,
    )
    # Definitively still restorable, so nothing was destroyed.
    assert (
        client.post(f"/api/v1/nodes/{node_id}/restore", headers=ALICE).status_code == HTTPStatus.OK
    )


# --- destruction -----------------------------------------------------------


def test_purging_reports_what_it_destroyed(client: TestClient) -> None:
    node_id = upload(client, ALICE, root_id(client, ALICE), "counted.bin")
    client.delete(f"/api/v1/nodes/{node_id}", headers=ALICE)

    response = client.post(f"/api/v1/nodes/{node_id}/purge", headers=ALICE)

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["purged"] == 1
    assert body["objects_deleted"] == 1
    assert body["bytes_reclaimed"] == len(PAYLOAD)


def test_a_purged_node_cannot_be_restored(client: TestClient) -> None:
    node_id = upload(client, ALICE, root_id(client, ALICE), "unrecoverable.bin")
    client.delete(f"/api/v1/nodes/{node_id}", headers=ALICE)
    client.post(f"/api/v1/nodes/{node_id}/purge", headers=ALICE)

    restored = client.post(f"/api/v1/nodes/{node_id}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.NOT_FOUND, restored.text


def test_purging_a_folder_takes_the_whole_subtree(client: TestClient) -> None:
    root = root_id(client, ALICE)
    outer = client.post(
        f"/api/v1/nodes/{root}/folders", json={"name": "outer"}, headers=ALICE
    ).json()["id"]
    inner = client.post(
        f"/api/v1/nodes/{outer}/folders", json={"name": "inner"}, headers=ALICE
    ).json()["id"]
    deep = upload(client, ALICE, inner, "deep.bin")
    shallow = upload(client, ALICE, outer, "shallow.bin")

    client.delete(f"/api/v1/nodes/{outer}", headers=ALICE)
    response = client.post(f"/api/v1/nodes/{outer}/purge", headers=ALICE)

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["purged"] == 4  # outer, inner, and two files
    assert body["objects_deleted"] == 2
    assert body["bytes_reclaimed"] == 2 * len(PAYLOAD)
    for node_id in (outer, inner, deep, shallow):
        assert client.get(f"/api/v1/nodes/{node_id}", headers=ALICE).status_code == (
            HTTPStatus.NOT_FOUND
        )


# --- quota -----------------------------------------------------------------


def test_purged_bytes_leave_the_row_totals(client: TestClient) -> None:
    node_id = upload(client, ALICE, root_id(client, ALICE), "space.bin")

    after_upload = usage(client, ALICE)
    assert after_upload["live_bytes"] == len(PAYLOAD)

    client.delete(f"/api/v1/nodes/{node_id}", headers=ALICE)
    after_delete = usage(client, ALICE)
    # A soft delete only moves the bytes; usage is unchanged.
    assert after_delete["trashed_bytes"] == len(PAYLOAD)
    assert after_delete["used_bytes"] == after_upload["used_bytes"]

    client.post(f"/api/v1/nodes/{node_id}/purge", headers=ALICE)
    after_purge = usage(client, ALICE)
    assert after_purge["trashed_bytes"] == 0
    assert after_purge["used_bytes"] == 0


# --- sharing ---------------------------------------------------------------


def test_a_purged_nodes_public_link_stops_resolving(client: TestClient) -> None:
    node_id = upload(client, ALICE, root_id(client, ALICE), "linked.bin")
    issued = client.post(f"/api/v1/nodes/{node_id}/links", json={}, headers=ALICE)
    assert issued.status_code == HTTPStatus.CREATED, issued.text
    token = issued.json()["token"]
    assert client.get(f"/api/v1/public/{token}/content").content == PAYLOAD

    client.delete(f"/api/v1/nodes/{node_id}", headers=ALICE)
    client.post(f"/api/v1/nodes/{node_id}/purge", headers=ALICE)

    after = client.get(f"/api/v1/public/{token}/content")
    assert after.status_code in (
        HTTPStatus.NOT_FOUND,
        HTTPStatus.GONE,
        HTTPStatus.FORBIDDEN,
    ), after.text


# --- schema ----------------------------------------------------------------


def test_the_route_is_published_with_its_error_responses(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/nodes/{node_id}/purge"]["post"]
    assert set(operation["responses"]) >= {"200", "404", "409"}
