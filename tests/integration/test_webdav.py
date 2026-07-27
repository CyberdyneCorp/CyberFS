"""The WebDAV surface against real Postgres and MinIO.

What only a real stack shows: that path resolution walks the tree correctly, that
a byte written over WebDAV is the same byte REST serves, and that delegation
actually carries the quota, encryption and trash rules rather than merely being
claimed to.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from collections.abc import Iterator
from http import HTTPStatus
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings, minio_endpoint

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
OCTET = "application/octet-stream"
DAV = "{DAV:}"
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
        minio_bucket=f"cyberfs-dav-{uuid.uuid4().hex[:8]}",
        minio_secure=False,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    response = client.get("/api/v1/nodes/root", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return str(response.json()["id"])


def dav_credentials(client: TestClient, who: dict[str, str] = ALICE) -> dict[str, str]:
    """Mint an access key over REST and present it as Basic, the way a client does."""
    root_id(client, who)  # provision the subject first
    minted = client.post("/api/v1/me/s3-keys", json={"label": "webdav"}, headers=who)
    assert minted.status_code == HTTPStatus.CREATED, minted.text
    body = minted.json()
    token = base64.b64encode(
        f"{body['access_key_id']}:{body['secret_access_key']}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def propfind(client: TestClient, path: str, creds: dict[str, str], depth: str = "1"):
    response = client.request("PROPFIND", path, headers={**creds, "Depth": depth})
    return response


def names_in(body: str) -> set[str]:
    root = ElementTree.fromstring(body)  # noqa: S314 - server output under test
    return {e.text or "" for e in root.iter(f"{DAV}displayname")}


# --- authentication --------------------------------------------------------


def test_a_minted_access_key_authenticates(client: TestClient) -> None:
    creds = dav_credentials(client)
    assert propfind(client, "/webdav", creds, depth="0").status_code == 207


def test_a_bearer_token_is_refused(client: TestClient) -> None:
    root_id(client, ALICE)
    response = propfind(client, "/webdav", ALICE, depth="0")
    assert response.status_code == HTTPStatus.UNAUTHORIZED, response.text


def test_a_revoked_key_stops_working_immediately(client: TestClient) -> None:
    root_id(client, ALICE)
    minted = client.post("/api/v1/me/s3-keys", json={"label": "doomed"}, headers=ALICE).json()
    token = base64.b64encode(
        f"{minted['access_key_id']}:{minted['secret_access_key']}".encode()
    ).decode()
    creds = {"Authorization": f"Basic {token}"}
    assert propfind(client, "/webdav", creds, depth="0").status_code == 207

    # The path takes the access key id; there is no separate `key_id` field.
    revoked = client.delete(f"/api/v1/me/s3-keys/{minted['access_key_id']}", headers=ALICE)
    assert revoked.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT), revoked.text

    assert propfind(client, "/webdav", creds, depth="0").status_code == HTTPStatus.UNAUTHORIZED


# --- PROPFIND --------------------------------------------------------------


def test_propfind_on_the_root_lists_the_callers_children(client: TestClient) -> None:
    creds = dav_credentials(client)
    root = root_id(client, ALICE)
    client.post(f"/api/v1/nodes/{root}/folders", json={"name": "papers"}, headers=ALICE)

    response = propfind(client, "/webdav", creds)

    assert response.status_code == 207, response.text
    assert "papers" in names_in(response.text)


def test_depth_infinity_is_refused(client: TestClient) -> None:
    creds = dav_credentials(client)
    assert propfind(client, "/webdav", creds, depth="infinity").status_code == HTTPStatus.FORBIDDEN


def test_a_trashed_node_is_absent(client: TestClient) -> None:
    creds = dav_credentials(client)
    root = root_id(client, ALICE)
    folder = client.post(
        f"/api/v1/nodes/{root}/folders", json={"name": "doomed"}, headers=ALICE
    ).json()["id"]
    client.delete(f"/api/v1/nodes/{folder}", headers=ALICE)

    assert "doomed" not in names_in(propfind(client, "/webdav", creds).text)
    assert propfind(client, "/webdav/doomed", creds).status_code == HTTPStatus.NOT_FOUND


def test_an_unknown_path_is_not_found(client: TestClient) -> None:
    creds = dav_credentials(client)
    assert propfind(client, "/webdav/nope/deeper", creds).status_code == HTTPStatus.NOT_FOUND


# --- content ---------------------------------------------------------------


def test_put_then_get_round_trips_bytes(client: TestClient) -> None:
    creds = dav_credentials(client)
    payload = os.urandom(4096)

    written = client.put(
        "/webdav/round.bin", content=payload, headers={**creds, "Content-Type": OCTET}
    )
    assert written.status_code == HTTPStatus.CREATED, written.text

    read = client.get("/webdav/round.bin", headers=creds)
    assert read.status_code == HTTPStatus.OK, read.text
    assert read.content == payload


def test_a_webdav_write_is_visible_over_rest_with_the_same_digest(client: TestClient) -> None:
    """The point of delegating: one surface cannot diverge from the other."""
    creds = dav_credentials(client)
    root = root_id(client, ALICE)
    payload = os.urandom(2048)
    client.put("/webdav/shared.bin", content=payload, headers={**creds, "Content-Type": OCTET})

    listing = client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()["items"]
    node = next(item for item in listing if item["name"] == "shared.bin")
    detail = client.get(f"/api/v1/nodes/{node['id']}", headers=ALICE).json()
    assert detail["digest"] == hashlib.sha256(payload).hexdigest()
    assert client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE).content == payload


def test_a_rest_write_is_readable_over_webdav(client: TestClient) -> None:
    creds = dav_credentials(client)
    root = root_id(client, ALICE)
    payload = os.urandom(1024)
    client.put(
        f"/api/v1/nodes/{root}/files/from-rest.bin",
        content=payload,
        headers={**ALICE, "Content-Type": OCTET},
    )

    assert client.get("/webdav/from-rest.bin", headers=creds).content == payload


def test_an_overwrite_becomes_a_new_version(client: TestClient) -> None:
    creds = dav_credentials(client)
    root = root_id(client, ALICE)
    first, second = os.urandom(512), os.urandom(700)
    client.put("/webdav/versioned.bin", content=first, headers={**creds, "Content-Type": OCTET})

    again = client.put(
        "/webdav/versioned.bin", content=second, headers={**creds, "Content-Type": OCTET}
    )
    assert again.status_code == HTTPStatus.NO_CONTENT, again.text
    assert client.get("/webdav/versioned.bin", headers=creds).content == second

    listing = client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()["items"]
    node = next(i for i in listing if i["name"] == "versioned.bin")
    versions = client.get(f"/api/v1/nodes/{node['id']}/versions", headers=ALICE).json()["items"]
    assert len(versions) >= 2


def test_encryption_inheritance_applies_to_a_webdav_upload(client: TestClient) -> None:
    """Delegation means the folder's default governs, whatever the surface."""
    creds = dav_credentials(client)
    root = root_id(client, ALICE)
    client.post(
        f"/api/v1/nodes/{root}/folders",
        json={"name": "vault", "encryption_default": "on"},
        headers=ALICE,
    )
    payload = os.urandom(3000)

    client.put(
        "/webdav/vault/sealed.bin", content=payload, headers={**creds, "Content-Type": OCTET}
    )

    vault = next(
        i
        for i in client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()["items"]
        if i["name"] == "vault"
    )
    child = client.get(f"/api/v1/nodes/{vault['id']}/children", headers=ALICE).json()["items"][0]
    assert child["encrypted"] is True
    assert client.get("/webdav/vault/sealed.bin", headers=creds).content == payload


def test_head_reports_length_without_a_body(client: TestClient) -> None:
    creds = dav_credentials(client)
    payload = os.urandom(300)
    client.put("/webdav/probe.bin", content=payload, headers={**creds, "Content-Type": OCTET})

    response = client.head("/webdav/probe.bin", headers=creds)
    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Length"] == str(len(payload))
    assert not response.content


# --- structure -------------------------------------------------------------


def test_mkcol_creates_a_folder_visible_over_rest(client: TestClient) -> None:
    creds = dav_credentials(client)
    root = root_id(client, ALICE)

    assert client.request("MKCOL", "/webdav/made", headers=creds).status_code == HTTPStatus.CREATED

    names = [
        i["name"]
        for i in client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()["items"]
    ]
    assert "made" in names


def test_mkcol_on_a_mapped_url_is_405_and_not_the_generic_412(client: TestClient) -> None:
    """RFC 4918 9.3.1 names 405 for a URL that is already mapped.

    Every other taken-name refusal on this surface is a 412, which is right for
    `COPY`/`MOVE` (9.8.5) and wrong here: a sync client calls `MKCOL` on
    directories that may already exist and reads 405 as "already there, carry
    on", where 412 is a precondition it never set. Caught against the live
    deployment, so it is pinned at this tier too.
    """
    creds = dav_credentials(client)
    assert client.request("MKCOL", "/webdav/twice", headers=creds).status_code == HTTPStatus.CREATED

    again = client.request("MKCOL", "/webdav/twice", headers=creds)

    assert again.status_code == HTTPStatus.METHOD_NOT_ALLOWED, again.text
    assert "{DAV:}error" in again.text or "D:error" in again.text, "must still be DAV XML"


def test_a_taken_name_is_still_412_for_move(client: TestClient) -> None:
    """The other half of the distinction: 412 stays correct where RFC 4918 says so."""
    creds = dav_credentials(client)
    client.put(
        "/webdav/source.bin", content=os.urandom(32), headers={**creds, "Content-Type": OCTET}
    )
    client.put(
        "/webdav/target.bin", content=os.urandom(32), headers={**creds, "Content-Type": OCTET}
    )

    moved = client.request(
        "MOVE",
        "/webdav/source.bin",
        headers={**creds, "Destination": "http://testserver/webdav/target.bin", "Overwrite": "F"},
    )

    assert moved.status_code == HTTPStatus.PRECONDITION_FAILED, moved.text


def test_delete_is_a_soft_delete_and_stays_restorable(client: TestClient) -> None:
    """WebDAV must not be a way around the trash."""
    creds = dav_credentials(client)
    root = root_id(client, ALICE)
    client.put("/webdav/gone.bin", content=os.urandom(64), headers={**creds, "Content-Type": OCTET})
    node = next(
        i
        for i in client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()["items"]
        if i["name"] == "gone.bin"
    )

    removed = client.request("DELETE", "/webdav/gone.bin", headers=creds)
    assert removed.status_code == HTTPStatus.NO_CONTENT, removed.text
    assert client.get("/webdav/gone.bin", headers=creds).status_code == HTTPStatus.NOT_FOUND

    restored = client.post(f"/api/v1/nodes/{node['id']}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK, restored.text
    assert client.get("/webdav/gone.bin", headers=creds).status_code == HTTPStatus.OK


def test_move_renames(client: TestClient) -> None:
    creds = dav_credentials(client)
    payload = os.urandom(128)
    client.put("/webdav/before.bin", content=payload, headers={**creds, "Content-Type": OCTET})

    moved = client.request(
        "MOVE",
        "/webdav/before.bin",
        headers={**creds, "Destination": "http://testserver/webdav/after.bin"},
    )
    assert moved.status_code == HTTPStatus.CREATED, moved.text
    assert client.get("/webdav/after.bin", headers=creds).content == payload
    assert client.get("/webdav/before.bin", headers=creds).status_code == HTTPStatus.NOT_FOUND


def test_copy_duplicates_and_leaves_the_original(client: TestClient) -> None:
    creds = dav_credentials(client)
    payload = os.urandom(256)
    client.put("/webdav/source.bin", content=payload, headers={**creds, "Content-Type": OCTET})
    client.request("MKCOL", "/webdav/dest", headers=creds)

    copied = client.request(
        "COPY",
        "/webdav/source.bin",
        headers={**creds, "Destination": "http://testserver/webdav/dest/source.bin"},
    )
    assert copied.status_code == HTTPStatus.CREATED, copied.text
    assert client.get("/webdav/dest/source.bin", headers=creds).content == payload
    assert client.get("/webdav/source.bin", headers=creds).content == payload


def test_an_unrequested_overwrite_is_refused(client: TestClient) -> None:
    creds = dav_credentials(client)
    client.put("/webdav/a.bin", content=b"a", headers={**creds, "Content-Type": OCTET})
    client.put("/webdav/b.bin", content=b"b", headers={**creds, "Content-Type": OCTET})

    refused = client.request(
        "MOVE",
        "/webdav/a.bin",
        headers={
            **creds,
            "Destination": "http://testserver/webdav/b.bin",
            "Overwrite": "F",
        },
    )
    assert refused.status_code == HTTPStatus.PRECONDITION_FAILED, refused.text
    assert client.get("/webdav/b.bin", headers=creds).content == b"b"


def test_a_destination_outside_the_surface_is_refused(client: TestClient) -> None:
    """Guessing what an unrelated URL meant is how a MOVE lands somewhere nobody asked for."""
    creds = dav_credentials(client)
    client.put("/webdav/here.bin", content=b"x", headers={**creds, "Content-Type": OCTET})

    response = client.request(
        "MOVE",
        "/webdav/here.bin",
        headers={**creds, "Destination": "http://elsewhere.test/other/here.bin"},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST, response.text


# --- isolation -------------------------------------------------------------


def test_another_callers_tree_is_unreachable(client: TestClient) -> None:
    """The walk starts at the caller's own root, so a path cannot leave it."""
    alice_creds = dav_credentials(client, ALICE)
    root_id(client, BOB)
    bob_root = root_id(client, BOB)
    client.post(f"/api/v1/nodes/{bob_root}/folders", json={"name": "bobs"}, headers=BOB)

    assert "bobs" not in names_in(propfind(client, "/webdav", alice_creds).text)
    assert propfind(client, "/webdav/bobs", alice_creds).status_code == HTTPStatus.NOT_FOUND


def test_a_name_cyberfs_rejects_is_refused(client: TestClient) -> None:
    creds = dav_credentials(client)
    response = client.request("MKCOL", "/webdav/shared", headers=creds)
    # `shared` is reserved at a tree root by the S3 namespace mapping.
    assert response.status_code == HTTPStatus.BAD_REQUEST, response.text
