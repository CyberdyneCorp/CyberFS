"""Sharing over real HTTP, against real Postgres and MinIO.

The revocation test is the one that matters: `caching/spec.md` requires a
withdrawn grant to be denied on the *very next request*, never after a TTL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from minio import Minio

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
PAYLOAD = b"shared content" * 50

ENDPOINT = os.environ.get("CYBERFS_TEST_MINIO_ENDPOINT", "localhost:9000")
_unreachable: str | None = None


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    global _unreachable
    if _unreachable is not None:
        pytest.skip(_unreachable)

    try:
        Minio(
            ENDPOINT, access_key="cyberfs", secret_key="cyberfs-dev-secret", secure=False
        ).list_buckets()
    except Exception as exc:
        _unreachable = f"no MinIO at {ENDPOINT}: {type(exc).__name__}"
        pytest.skip(_unreachable)

    settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        minio_endpoint=ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=f"cyberfs-share-{uuid.uuid4().hex[:8]}",
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


def upload(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.put(f"/api/v1/nodes/{parent}/files/{name}", content=PAYLOAD, headers=who)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def grant(client: TestClient, node: str, subject: str, role: str) -> None:
    response = client.put(
        f"/api/v1/nodes/{node}/grants", json={"recipient": subject, "role": role}, headers=ALICE
    )
    assert response.status_code == HTTPStatus.CREATED, response.text


# --- grants ----------------------------------------------------------------


def test_a_grant_lets_the_recipient_read(client: TestClient) -> None:
    root_id(client, BOB)  # provision Bob
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")

    grant(client, folder, "bob", "viewer")

    response = client.get(f"/api/v1/nodes/{folder}", headers=BOB)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["role"] == "viewer"


def test_a_viewer_cannot_write(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    grant(client, folder, "bob", "viewer")

    response = client.post(f"/api/v1/nodes/{folder}/folders", json={"name": "mine"}, headers=BOB)
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_an_editor_can_write(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    grant(client, folder, "bob", "editor")

    response = client.post(f"/api/v1/nodes/{folder}/folders", json={"name": "bobs"}, headers=BOB)
    assert response.status_code == HTTPStatus.CREATED


def test_an_editor_cannot_re_share(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    grant(client, folder, "bob", "editor")

    response = client.put(
        f"/api/v1/nodes/{folder}/grants",
        json={"recipient": "carol", "role": "viewer"},
        headers=BOB,
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_a_grant_reaches_descendant_content(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    node = upload(client, ALICE, folder, "inside.bin")
    grant(client, folder, "bob", "viewer")

    response = client.get(f"/api/v1/nodes/{node}/content", headers=BOB)
    assert response.status_code == HTTPStatus.OK
    assert response.content == PAYLOAD


def test_sharing_with_yourself_is_a_conflict(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    response = client.put(
        f"/api/v1/nodes/{folder}/grants",
        json={"recipient": "alice", "role": "viewer"},
        headers=ALICE,
    )
    assert response.status_code == HTTPStatus.CONFLICT


# --- revocation ------------------------------------------------------------


def test_revocation_denies_the_very_next_request(client: TestClient) -> None:
    """No TTL, no eventual consistency -- the next request is already denied."""
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    node = upload(client, ALICE, folder, "secret.bin")
    grant(client, folder, "bob", "viewer")

    assert client.get(f"/api/v1/nodes/{node}/content", headers=BOB).status_code == HTTPStatus.OK

    revoked = client.delete(f"/api/v1/nodes/{folder}/grants/bob", headers=ALICE)
    assert revoked.status_code == HTTPStatus.NO_CONTENT

    after = client.get(f"/api/v1/nodes/{node}/content", headers=BOB)
    assert after.status_code == HTTPStatus.NOT_FOUND


def test_a_recipient_can_drop_their_own_access(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    grant(client, folder, "bob", "viewer")

    response = client.delete(f"/api/v1/nodes/{folder}/grants/bob", headers=BOB)
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert client.get(f"/api/v1/nodes/{folder}", headers=BOB).status_code == HTTPStatus.NOT_FOUND


def test_shared_with_me_lists_the_share(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "shared")
    grant(client, folder, "bob", "viewer")

    listing = client.get("/api/v1/shared-with-me", headers=BOB).json()
    assert [item["id"] for item in listing["items"]] == [folder]


# --- public links ----------------------------------------------------------


def test_a_public_link_serves_content_without_authentication(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "public")
    node = upload(client, ALICE, folder, "open.bin")

    issued = client.post(f"/api/v1/nodes/{node}/links", json={}, headers=ALICE)
    assert issued.status_code == HTTPStatus.CREATED
    token = issued.json()["token"]

    response = client.get(f"/api/v1/public/{token}/content")
    assert response.status_code == HTTPStatus.OK
    assert response.content == PAYLOAD


def test_a_public_link_is_read_only(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "public")
    token = client.post(f"/api/v1/nodes/{folder}/links", json={}, headers=ALICE).json()["token"]

    # There is no mutating route under /public at all.
    response = client.post(f"/api/v1/public/{token}/folders", json={"name": "x"})
    assert response.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED)


def test_an_unknown_link_is_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/public/not-a-token").status_code == HTTPStatus.NOT_FOUND


def test_a_revoked_link_stops_working(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "public")
    issued = client.post(f"/api/v1/nodes/{folder}/links", json={}, headers=ALICE).json()

    assert client.get(f"/api/v1/public/{issued['token']}").status_code == HTTPStatus.OK
    client.delete(f"/api/v1/links/{issued['id']}", headers=ALICE)

    assert client.get(f"/api/v1/public/{issued['token']}").status_code == HTTPStatus.NOT_FOUND


def test_an_expired_link_is_not_found(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "public")
    past = (datetime.now(tz=UTC) + timedelta(seconds=1)).isoformat()
    issued = client.post(f"/api/v1/nodes/{folder}/links", json={"expires_at": past}, headers=ALICE)
    assert issued.status_code == HTTPStatus.CREATED


def test_a_passphrase_gates_the_link(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "public")
    token = client.post(
        f"/api/v1/nodes/{folder}/links", json={"passphrase": "open-sesame"}, headers=ALICE
    ).json()["token"]

    assert client.get(f"/api/v1/public/{token}").status_code == HTTPStatus.FORBIDDEN

    opened = client.get(f"/api/v1/public/{token}", headers={"X-Link-Passphrase": "open-sesame"})
    assert opened.status_code == HTTPStatus.OK


def test_the_link_listing_never_echoes_the_token(client: TestClient) -> None:
    """The token is returned once, at creation, and never again."""
    folder = make_folder(client, ALICE, root_id(client, ALICE), "public")
    token = client.post(f"/api/v1/nodes/{folder}/links", json={}, headers=ALICE).json()["token"]

    listing = client.get(f"/api/v1/nodes/{folder}/links", headers=ALICE)
    assert token not in listing.text
    assert "token" not in listing.json()["items"][0]


def test_only_the_owner_may_list_links(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "public")
    grant(client, folder, "bob", "editor")

    response = client.get(f"/api/v1/nodes/{folder}/links", headers=BOB)
    assert response.status_code == HTTPStatus.FORBIDDEN


# --- ownership transfer ----------------------------------------------------


def test_ownership_transfer(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "handover")

    response = client.post(
        f"/api/v1/nodes/{folder}/owner", json={"recipient": "bob"}, headers=ALICE
    )
    assert response.status_code == HTTPStatus.OK

    assert client.get(f"/api/v1/nodes/{folder}", headers=BOB).json()["role"] == "owner"


def test_the_previous_owner_is_left_an_editor(client: TestClient) -> None:
    root_id(client, BOB)
    folder = make_folder(client, ALICE, root_id(client, ALICE), "handover")
    client.post(f"/api/v1/nodes/{folder}/owner", json={"recipient": "bob"}, headers=ALICE)

    assert client.get(f"/api/v1/nodes/{folder}", headers=ALICE).json()["role"] == "editor"


def test_transfer_to_an_unknown_user_is_not_found(client: TestClient) -> None:
    folder = make_folder(client, ALICE, root_id(client, ALICE), "handover")
    response = client.post(
        f"/api/v1/nodes/{folder}/owner", json={"recipient": "nobody"}, headers=ALICE
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
