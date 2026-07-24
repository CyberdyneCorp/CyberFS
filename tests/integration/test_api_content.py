"""Upload and download over real HTTP, against real Postgres and MinIO."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
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
PAYLOAD = b"the quick brown fox jumps over the lazy dog\n" * 100

ENDPOINT = os.environ.get("CYBERFS_TEST_MINIO_ENDPOINT", "localhost:9000")
_unreachable: str | None = None


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    global _unreachable
    if _unreachable is not None:
        pytest.skip(_unreachable)

    bucket = f"cyberfs-api-{uuid.uuid4().hex[:8]}"
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
        minio_bucket=bucket,
        minio_secure=False,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    return str(client.get("/api/v1/nodes/root", headers=who).json()["id"])


def upload(
    client: TestClient, who: dict[str, str], parent: str, name: str, body: bytes
) -> dict[str, object]:
    response = client.put(
        f"/api/v1/nodes/{parent}/files/{name}",
        content=body,
        headers={**who, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return dict(response.json())


# --- round trip ------------------------------------------------------------


def test_upload_then_download(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "notes.txt", PAYLOAD)

    response = client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE)

    assert response.status_code == HTTPStatus.OK
    assert response.content == PAYLOAD
    assert response.headers["Content-Length"] == str(len(PAYLOAD))


def test_an_empty_file_round_trips(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "empty.txt", b"")
    response = client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE)
    assert response.content == b""


def test_binary_content_survives(client: TestClient) -> None:
    payload = bytes(range(256)) * 100
    node = upload(client, ALICE, root_id(client, ALICE), "data.bin", payload)
    assert client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE).content == payload


def test_the_uploaded_file_appears_in_its_folder(client: TestClient) -> None:
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "listed.txt", PAYLOAD)

    listing = client.get(f"/api/v1/nodes/{root}/children", headers=ALICE).json()
    assert [item["id"] for item in listing["items"]] == [node["id"]]
    assert listing["items"][0]["size_bytes"] == len(PAYLOAD)


# --- ranges ----------------------------------------------------------------


def test_a_range_request_returns_partial_content(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "ranged.bin", PAYLOAD)

    response = client.get(
        f"/api/v1/nodes/{node['id']}/content", headers={**ALICE, "Range": "bytes=10-19"}
    )

    assert response.status_code == HTTPStatus.PARTIAL_CONTENT
    assert response.content == PAYLOAD[10:20]
    assert response.headers["Content-Range"] == f"bytes 10-19/{len(PAYLOAD)}"


def test_a_suffix_range(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "ranged.bin", PAYLOAD)
    response = client.get(
        f"/api/v1/nodes/{node['id']}/content", headers={**ALICE, "Range": "bytes=-10"}
    )
    assert response.content == PAYLOAD[-10:]


def test_an_unsatisfiable_range_serves_the_whole_object(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "ranged.bin", PAYLOAD)
    response = client.get(
        f"/api/v1/nodes/{node['id']}/content", headers={**ALICE, "Range": "bytes=999999-"}
    )
    assert response.status_code == HTTPStatus.OK
    assert response.content == PAYLOAD


def test_ranges_are_advertised(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "a.bin", PAYLOAD)
    response = client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE)
    assert response.headers["Accept-Ranges"] == "bytes"


# --- no presigned URLs -----------------------------------------------------


def test_no_response_ever_carries_a_presigned_url(client: TestClient) -> None:
    """`file-storage/spec.md`: direct object access is never delegated."""
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "a.bin", PAYLOAD)

    surfaces = [
        client.get(f"/api/v1/nodes/{node['id']}", headers=ALICE),
        client.get(f"/api/v1/nodes/{root}/children", headers=ALICE),
        client.get(f"/api/v1/nodes/{node['id']}/versions", headers=ALICE),
    ]

    for response in surfaces:
        body = response.text.lower()
        for marker in ("x-amz-signature", "presign", "amz-credential", ENDPOINT.lower()):
            assert marker not in body, f"{marker} leaked into {response.url}"


def test_download_headers_carry_no_object_key(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "a.bin", PAYLOAD)
    response = client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE)

    headers = " ".join(f"{k}:{v}" for k, v in response.headers.items()).lower()
    assert "location" not in response.headers
    assert str(node["id"]).lower() not in headers.replace("etag:", "")


# --- authorization ---------------------------------------------------------


def test_downloading_someone_elses_file_is_not_found(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "private.bin", PAYLOAD)
    response = client.get(f"/api/v1/nodes/{node['id']}/content", headers=BOB)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_uploading_into_someone_elses_folder_is_not_found(client: TestClient) -> None:
    alice_root = root_id(client, ALICE)
    response = client.put(f"/api/v1/nodes/{alice_root}/files/sneaky.txt", content=b"x", headers=BOB)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_unauthenticated_upload_is_refused(client: TestClient) -> None:
    root = root_id(client, ALICE)
    response = client.put(f"/api/v1/nodes/{root}/files/a.txt", content=b"x")
    assert response.status_code == HTTPStatus.UNAUTHORIZED


# --- versions --------------------------------------------------------------


def test_replacing_content_creates_a_version(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "a.txt", b"first")

    replaced = client.put(f"/api/v1/nodes/{node['id']}/content", content=b"second", headers=ALICE)
    assert replaced.status_code == HTTPStatus.OK

    versions = client.get(f"/api/v1/nodes/{node['id']}/versions", headers=ALICE).json()
    assert len(versions["items"]) == 2
    assert versions["items"][0]["is_current"]


def test_downloading_an_earlier_version(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "a.txt", b"first")
    client.put(f"/api/v1/nodes/{node['id']}/content", content=b"second", headers=ALICE)

    versions = client.get(f"/api/v1/nodes/{node['id']}/versions", headers=ALICE).json()
    oldest = versions["items"][-1]["id"]

    response = client.get(f"/api/v1/nodes/{node['id']}/content?version={oldest}", headers=ALICE)
    assert response.content == b"first"


def test_restoring_a_version_makes_it_current(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "a.txt", b"first")
    client.put(f"/api/v1/nodes/{node['id']}/content", content=b"second", headers=ALICE)
    versions = client.get(f"/api/v1/nodes/{node['id']}/versions", headers=ALICE).json()
    oldest = versions["items"][-1]["id"]

    restored = client.post(f"/api/v1/nodes/{node['id']}/versions/{oldest}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK

    assert client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE).content == b"first"


def test_version_history_carries_no_object_keys(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "a.txt", PAYLOAD)
    body = client.get(f"/api/v1/nodes/{node['id']}/versions", headers=ALICE).text
    assert "object_key" not in body
    assert str(node["owner_id"]) not in body if "owner_id" in node else True


# --- copy with content -----------------------------------------------------


def test_copying_a_file_duplicates_its_bytes(client: TestClient) -> None:
    root = root_id(client, ALICE)
    source = upload(client, ALICE, root, "source.bin", PAYLOAD)
    target = client.post(
        f"/api/v1/nodes/{root}/folders", json={"name": "target"}, headers=ALICE
    ).json()

    copied = client.post(
        f"/api/v1/nodes/{source['id']}/copy",
        json={"parent_id": str(target["id"])},
        headers=ALICE,
    )
    assert copied.status_code == HTTPStatus.CREATED

    response = client.get(f"/api/v1/nodes/{copied.json()['id']}/content", headers=ALICE)
    assert response.content == PAYLOAD


# --- trash -----------------------------------------------------------------


def test_deleting_a_file_hides_its_content(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "temp.bin", PAYLOAD)
    client.delete(f"/api/v1/nodes/{node['id']}", headers=ALICE)

    response = client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_restoring_brings_the_content_back(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "temp.bin", PAYLOAD)
    client.delete(f"/api/v1/nodes/{node['id']}", headers=ALICE)
    client.post(f"/api/v1/nodes/{node['id']}/restore", headers=ALICE)

    response = client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE)
    assert response.content == PAYLOAD
