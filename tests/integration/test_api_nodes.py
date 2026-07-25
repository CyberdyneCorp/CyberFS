"""The filesystem endpoints, end to end.

Real HTTP, real Postgres, real transactions. Auth runs in dev mode so these
exercise the filesystem rather than re-testing token verification.
"""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    """An app wired to the integration database, with the auth stub enabled."""
    settings = build_settings(auth_dev_mode=True, environment=Environment.TEST)
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    response = client.get("/api/v1/nodes/root", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return str(response.json()["id"])


def make_folder(
    client: TestClient, who: dict[str, str], parent: str, name: str
) -> dict[str, object]:
    response = client.post(f"/api/v1/nodes/{parent}/folders", json={"name": name}, headers=who)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return dict(response.json())


# --- provisioning ----------------------------------------------------------


def test_first_request_provisions_a_root(client: TestClient) -> None:
    response = client.get("/api/v1/nodes/root", headers=ALICE)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["kind"] == "folder"
    assert body["path"] == "/"
    assert body["role"] == "owner"


def test_the_root_is_stable_across_requests(client: TestClient) -> None:
    assert root_id(client, ALICE) == root_id(client, ALICE)


def test_distinct_callers_get_distinct_roots(client: TestClient) -> None:
    assert root_id(client, ALICE) != root_id(client, BOB)


def test_unauthenticated_requests_are_refused(client: TestClient) -> None:
    assert client.get("/api/v1/nodes/root").status_code == HTTPStatus.UNAUTHORIZED


# --- create and list -------------------------------------------------------


def test_folder_is_created_and_listed(client: TestClient) -> None:
    root = root_id(client, ALICE)
    created = make_folder(client, ALICE, root, "reports")

    listing = client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()
    assert [item["id"] for item in listing["items"]] == [created["id"]]


def test_created_folder_reports_its_path(client: TestClient) -> None:
    root = root_id(client, ALICE)
    outer = make_folder(client, ALICE, root, "a")
    inner = make_folder(client, ALICE, str(outer["id"]), "b")
    assert inner["path"] == "/a/b"


def test_duplicate_name_is_a_conflict(client: TestClient) -> None:
    root = root_id(client, ALICE)
    make_folder(client, ALICE, root, "reports")

    response = client.post(f"/api/v1/nodes/{root}/folders", json={"name": "reports"}, headers=ALICE)
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "name_taken"


@pytest.mark.parametrize("name", ["a/b", ".", "..", "a\\b"])
def test_invalid_names_are_rejected(client: TestClient, name: str) -> None:
    root = root_id(client, ALICE)
    response = client.post(f"/api/v1/nodes/{root}/folders", json={"name": name}, headers=ALICE)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_empty_name_is_rejected_by_the_schema(client: TestClient) -> None:
    root = root_id(client, ALICE)
    response = client.post(f"/api/v1/nodes/{root}/folders", json={"name": ""}, headers=ALICE)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    root = root_id(client, ALICE)
    response = client.post(
        f"/api/v1/nodes/{root}/folders",
        json={"name": "ok", "owner_id": "someone-else"},
        headers=ALICE,
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_creation_persists_across_requests(client: TestClient) -> None:
    """Proves the transaction actually committed."""
    root = root_id(client, ALICE)
    created = make_folder(client, ALICE, root, "durable")

    fetched = client.get(f"/api/v1/nodes/{created['id']}", headers=ALICE)
    assert fetched.status_code == HTTPStatus.OK
    assert fetched.json()["name"] == "durable"


def test_listing_pages(client: TestClient) -> None:
    root = root_id(client, ALICE)
    for i in range(5):
        make_folder(client, ALICE, root, f"folder-{i}")

    first = client.get(f"/api/v1/nodes/{root}/children?limit=2", headers=ALICE).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second = client.get(
        f"/api/v1/nodes/{root}/children?limit=2&cursor={first['next_cursor']}", headers=ALICE
    ).json()
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


# --- isolation -------------------------------------------------------------


def test_another_callers_node_is_not_found(client: TestClient) -> None:
    """404 rather than 403, so existence is not disclosed."""
    hers = make_folder(client, ALICE, root_id(client, ALICE), "private")
    response = client.get(f"/api/v1/nodes/{hers['id']}", headers=BOB)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_another_callers_root_is_not_listable(client: TestClient) -> None:
    alice_root = root_id(client, ALICE)
    response = client.get(f"/api/v1/nodes/{alice_root}/children", headers=BOB)
    assert response.status_code == HTTPStatus.NOT_FOUND


# --- rename, move, etags ---------------------------------------------------


def test_rename(client: TestClient) -> None:
    created = make_folder(client, ALICE, root_id(client, ALICE), "old")
    response = client.patch(
        f"/api/v1/nodes/{created['id']}/name", json={"name": "new"}, headers=ALICE
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["name"] == "new"


def test_etag_is_published_and_accepted(client: TestClient) -> None:
    created = make_folder(client, ALICE, root_id(client, ALICE), "a")
    fetched = client.get(f"/api/v1/nodes/{created['id']}", headers=ALICE)
    etag = fetched.headers["ETag"]

    response = client.patch(
        f"/api/v1/nodes/{created['id']}/name",
        json={"name": "b"},
        headers={**ALICE, "If-Match": etag},
    )
    assert response.status_code == HTTPStatus.OK


def test_stale_etag_is_a_precondition_failure(client: TestClient) -> None:
    created = make_folder(client, ALICE, root_id(client, ALICE), "a")
    stale = client.get(f"/api/v1/nodes/{created['id']}", headers=ALICE).headers["ETag"]
    client.patch(f"/api/v1/nodes/{created['id']}/name", json={"name": "b"}, headers=ALICE)

    response = client.patch(
        f"/api/v1/nodes/{created['id']}/name",
        json={"name": "c"},
        headers={**ALICE, "If-Match": stale},
    )
    assert response.status_code == HTTPStatus.PRECONDITION_FAILED


def test_move(client: TestClient) -> None:
    root = root_id(client, ALICE)
    target = make_folder(client, ALICE, root, "target")
    moving = make_folder(client, ALICE, root, "moving")

    response = client.patch(
        f"/api/v1/nodes/{moving['id']}/parent",
        json={"parent_id": str(target["id"])},
        headers=ALICE,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["path"] == "/target/moving"


def test_move_into_own_descendant_is_a_conflict(client: TestClient) -> None:
    root = root_id(client, ALICE)
    outer = make_folder(client, ALICE, root, "outer")
    inner = make_folder(client, ALICE, str(outer["id"]), "inner")

    response = client.patch(
        f"/api/v1/nodes/{outer['id']}/parent",
        json={"parent_id": str(inner["id"])},
        headers=ALICE,
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "would_create_cycle"


def test_renaming_a_folder_moves_descendant_paths(client: TestClient) -> None:
    root = root_id(client, ALICE)
    folder = make_folder(client, ALICE, root, "reports")
    leaf = make_folder(client, ALICE, str(folder["id"]), "q3")

    client.patch(f"/api/v1/nodes/{folder['id']}/name", json={"name": "archive"}, headers=ALICE)

    response = client.get(f"/api/v1/nodes/{leaf['id']}", headers=ALICE)
    assert response.json()["path"] == "/archive/q3"


# --- copy ------------------------------------------------------------------


def test_copy_duplicates_a_subtree(client: TestClient) -> None:
    root = root_id(client, ALICE)
    source = make_folder(client, ALICE, root, "source")
    make_folder(client, ALICE, str(source["id"]), "nested")
    target = make_folder(client, ALICE, root, "target")

    response = client.post(
        f"/api/v1/nodes/{source['id']}/copy",
        json={"parent_id": str(target["id"])},
        headers=ALICE,
    )
    assert response.status_code == HTTPStatus.CREATED

    children = client.get(f"/api/v1/nodes/{response.json()['id']}/children", headers=ALICE).json()
    assert [c["name"] for c in children["items"]] == ["nested"]


def test_copy_into_the_same_folder_needs_a_name(client: TestClient) -> None:
    root = root_id(client, ALICE)
    source = make_folder(client, ALICE, root, "source")

    response = client.post(
        f"/api/v1/nodes/{source['id']}/copy",
        json={"parent_id": root, "name": "source-copy"},
        headers=ALICE,
    )
    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["name"] == "source-copy"


# --- trash -----------------------------------------------------------------


def test_delete_then_restore(client: TestClient) -> None:
    root = root_id(client, ALICE)
    created = make_folder(client, ALICE, root, "temp")

    deleted = client.delete(f"/api/v1/nodes/{created['id']}", headers=ALICE)
    assert deleted.status_code == HTTPStatus.OK
    assert deleted.json()["deleted"] == 1

    assert (
        client.get(f"/api/v1/nodes/{created['id']}", headers=ALICE).status_code
        == HTTPStatus.NOT_FOUND
    )

    restored = client.post(f"/api/v1/nodes/{created['id']}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK
    assert client.get(f"/api/v1/nodes/{created['id']}", headers=ALICE).status_code == HTTPStatus.OK


def test_delete_covers_the_subtree(client: TestClient) -> None:
    root = root_id(client, ALICE)
    outer = make_folder(client, ALICE, root, "outer")
    make_folder(client, ALICE, str(outer["id"]), "inner")

    response = client.delete(f"/api/v1/nodes/{outer['id']}", headers=ALICE)
    assert response.json()["deleted"] == 2


def test_a_deleted_name_becomes_reusable(client: TestClient) -> None:
    """The partial unique index, observed through the API."""
    root = root_id(client, ALICE)
    first = make_folder(client, ALICE, root, "recycled")
    client.delete(f"/api/v1/nodes/{first['id']}", headers=ALICE)

    second = make_folder(client, ALICE, root, "recycled")
    assert second["id"] != first["id"]


def test_root_cannot_be_deleted(client: TestClient) -> None:
    root = root_id(client, ALICE)
    response = client.delete(f"/api/v1/nodes/{root}", headers=ALICE)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# --- search ----------------------------------------------------------------


def test_search_finds_by_name(client: TestClient) -> None:
    root = root_id(client, ALICE)
    make_folder(client, ALICE, root, "quarterly-reports")
    make_folder(client, ALICE, root, "photos")

    results = client.get("/api/v1/search?q=report", headers=ALICE).json()
    assert [r["name"] for r in results["items"]] == ["quarterly-reports"]


def test_search_is_scoped_to_the_caller(client: TestClient) -> None:
    make_folder(client, ALICE, root_id(client, ALICE), "alice-secret")
    results = client.get("/api/v1/search?q=secret", headers=BOB).json()
    assert results["items"] == []


def test_search_requires_a_query(client: TestClient) -> None:
    assert client.get("/api/v1/search", headers=ALICE).status_code == (
        HTTPStatus.UNPROCESSABLE_ENTITY
    )


# --- responses -------------------------------------------------------------


def test_responses_carry_no_key_material(client: TestClient) -> None:
    root = root_id(client, ALICE)
    body = client.get(f"/api/v1/nodes/{root}", headers=ALICE).text.lower()
    for forbidden in ("wrapped", "kek", "dek", "master_key", "secret"):
        assert forbidden not in body


def test_folder_response_omits_file_only_fields(client: TestClient) -> None:
    created = make_folder(client, ALICE, root_id(client, ALICE), "a")
    assert created["content_type"] is None
    assert created["encrypted"] is False
    assert created["encryption_default"] == "inherit"
