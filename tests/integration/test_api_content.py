"""Upload and download over real HTTP, against real Postgres and MinIO."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from minio import Minio

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings, minio_endpoint

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
ADMIN = {"Authorization": "Bearer dev:root:admin"}
PAYLOAD = b"the quick brown fox jumps over the lazy dog\n" * 100

ENDPOINT = minio_endpoint()
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


#: The internal object-key shape -- three UUIDs -- naming raw stored bytes. It
#: must never appear in any response (`file-storage/spec.md`, sharpened rule).
_OBJECT_KEY_PATTERN = re.compile(r"[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}", re.IGNORECASE)


def test_no_response_ever_carries_a_presigned_url(client: TestClient) -> None:
    """`file-storage/spec.md`, sharpened rule (task 9.5): direct MinIO access is
    never delegated. What is banned is the object-store endpoint host, a
    MinIO-signed URL, and an internal object key -- NOT a CyberFS-endpoint
    presigned URL, which is now explicitly permitted because the bytes still
    transit CyberFS."""
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "a.bin", PAYLOAD)

    surfaces = [
        client.get(f"/api/v1/nodes/{node['id']}", headers=ALICE),
        client.get(f"/api/v1/nodes/{root}/children", headers=ALICE),
        client.get(f"/api/v1/nodes/{node['id']}/versions", headers=ALICE),
    ]

    for response in surfaces:
        body = response.text.lower()
        # The MinIO endpoint and any URL signed for it are forbidden; a bare
        # x-amz-signature is not, since a CyberFS-endpoint presigned URL is now
        # permitted.
        assert ENDPOINT.lower() not in body, f"MinIO endpoint leaked into {response.url}"
        assert "minio" not in body, f"MinIO reference leaked into {response.url}"
        assert not _OBJECT_KEY_PATTERN.search(response.text), (
            f"internal object key leaked into {response.url}"
        )


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


def test_restoring_a_folder_brings_its_file_back(client: TestClient) -> None:
    """`file-storage/spec.md`: the folder is visible in its parent again, and
    so is what it held.

    The admin figures below are row aggregates -- `SqlAdminQueries._node_counts`
    sums `nodes.size_bytes`, it does not read `quota_usage` -- so they restate
    what the listing already showed rather than proving anything about the
    counters. The bucket accounting is covered where it can actually be observed:
    `tests/unit/test_node_service.py` compares against `quotas.recompute`, and
    `tests/integration/test_repositories.py` checks the rows against real SQL.
    """
    root = root_id(client, ALICE)
    folder = client.post(
        f"/api/v1/nodes/{root}/folders", json={"name": "papers"}, headers=ALICE
    ).json()
    node = upload(client, ALICE, str(folder["id"]), "notes.txt", PAYLOAD)

    assert client.delete(f"/api/v1/nodes/{folder['id']}", headers=ALICE).json()["deleted"] == 2
    restored = client.post(f"/api/v1/nodes/{folder['id']}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK

    listing = client.get(f"/api/v1/nodes/{folder['id']}/children", headers=ALICE).json()
    assert [item["id"] for item in listing["items"]] == [node["id"]]
    assert client.get(f"/api/v1/nodes/{node['id']}/content", headers=ALICE).content == PAYLOAD

    users = client.get("/api/v1/admin/users", headers=ADMIN).json()["items"]
    alice = next(u for u in users if u["subject"] == "alice")
    assert alice["live_bytes"] == len(PAYLOAD)
    assert alice["trashed_bytes"] == 0
    assert alice["used_bytes"] == len(PAYLOAD)


# --- encryption ------------------------------------------------------------


def test_an_encrypted_upload_round_trips(client: TestClient) -> None:
    root = root_id(client, ALICE)
    response = client.put(
        f"/api/v1/nodes/{root}/files/secret.bin?encrypted=true",
        content=PAYLOAD,
        headers=ALICE,
    )
    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["encrypted"] is True

    node = response.json()["id"]
    fetched = client.get(f"/api/v1/nodes/{node}/content", headers=ALICE)
    assert fetched.content == PAYLOAD
    assert fetched.headers["Content-Length"] == str(len(PAYLOAD))


def test_an_encrypted_file_accepts_a_second_version_and_both_stay_readable(
    client: TestClient,
) -> None:
    """Replacing an encrypted file's content, which never worked until now.

    `_seal_if_needed` minted a fresh DEK on every write, but `wrapped_data_keys`
    is unique on `(node_id, subject)` and has no version column -- so the second
    write collided on `uq_wrapped_data_keys_node_subject`, which the unit of work
    translates into a bare `ConflictError`, and the request answered `409`.
    Nothing caught it: every existing test either uploads an encrypted file once
    or toggles encryption on a plaintext one, and both mint their key for the
    first time.

    The second assertion is the one that matters for data safety. Reusing the
    node's DEK is only correct if it keeps *history* readable, so the previous
    version is read back through the version endpoint and compared byte for byte.
    """
    root = root_id(client, ALICE)
    created = client.put(
        f"/api/v1/nodes/{root}/files/rewritten.bin?encrypted=true",
        content=PAYLOAD,
        headers=ALICE,
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    node = created.json()["id"]

    second = PAYLOAD + b"-the-sequel"
    replaced = client.put(f"/api/v1/nodes/{node}/content", content=second, headers=ALICE)
    assert replaced.status_code == HTTPStatus.OK, replaced.text

    assert client.get(f"/api/v1/nodes/{node}/content", headers=ALICE).content == second

    versions = client.get(f"/api/v1/nodes/{node}/versions", headers=ALICE).json()["items"]
    assert len(versions) == 2, versions
    assert {v["sequence"] for v in versions} == {1, 2}
    assert all(v["encrypted"] for v in versions), versions


def test_restoring_a_version_of_an_encrypted_file_returns_its_bytes(client: TestClient) -> None:
    """The regression test for the copy-sealing defect, over real MinIO.

    `seal` binds the version id into the AEAD, and a restore writes a new row for
    the source's bytes. Authenticating against the new row's own id failed the
    tag, so the download returned an empty body while the restore had already
    repointed `current_version_id` -- a rollback that silently emptied the file.
    """
    root = root_id(client, ALICE)
    created = client.put(
        f"/api/v1/nodes/{root}/files/rolled-back.bin?encrypted=true",
        content=PAYLOAD,
        headers=ALICE,
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    node = created.json()["id"]
    client.put(f"/api/v1/nodes/{node}/content", content=PAYLOAD + b"-v2", headers=ALICE)

    versions = client.get(f"/api/v1/nodes/{node}/versions", headers=ALICE).json()["items"]
    first = next(v for v in versions if v["sequence"] == 1)
    restored = client.post(f"/api/v1/nodes/{node}/versions/{first['id']}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK, restored.text

    assert client.get(f"/api/v1/nodes/{node}/content", headers=ALICE).content == PAYLOAD
    # The digest describes plaintext, so restoring the same content restores the
    # same digest -- a re-seal under a new key would keep this true, which is why
    # the byte comparison above is the load-bearing assertion.
    assert client.get(f"/api/v1/nodes/{node}", headers=ALICE).json()["digest"] == (
        hashlib.sha256(PAYLOAD).hexdigest()
    )


def test_copying_an_encrypted_file_leaves_the_copy_readable(client: TestClient) -> None:
    """`POST /nodes/{id}/copy` on an encrypted file was doubly broken.

    The copy carried neither the sealing id that authenticates its bytes nor a
    wrapped key against its own node id, so it had ciphertext, a version row, and
    no way to open either.
    """
    root = root_id(client, ALICE)
    folder = client.post(
        f"/api/v1/nodes/{root}/folders", json={"name": "copies"}, headers=ALICE
    ).json()["id"]
    created = client.put(
        f"/api/v1/nodes/{root}/files/original.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    )
    node = created.json()["id"]

    copied = client.post(
        f"/api/v1/nodes/{node}/copy", json={"parent_id": folder}, headers=ALICE
    )
    assert copied.status_code == HTTPStatus.CREATED, copied.text
    copy_id = copied.json()["id"]

    assert client.get(f"/api/v1/nodes/{copy_id}/content", headers=ALICE).content == PAYLOAD
    assert client.get(f"/api/v1/nodes/{copy_id}", headers=ALICE).json()["encrypted"] is True


def test_a_version_of_a_copied_encrypted_file_still_restores(client: TestClient) -> None:
    """The transitive case: a copy of a copy, which no plaintext test reaches.

    The copy carries its source's *sealing* id rather than the source's own id,
    so this works with no chain to walk at read time.
    """
    root = root_id(client, ALICE)
    folder = client.post(
        f"/api/v1/nodes/{root}/folders", json={"name": "chain"}, headers=ALICE
    ).json()["id"]
    node = client.put(
        f"/api/v1/nodes/{root}/files/chained.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    ).json()["id"]
    copy_id = client.post(
        f"/api/v1/nodes/{node}/copy", json={"parent_id": folder}, headers=ALICE
    ).json()["id"]

    client.put(f"/api/v1/nodes/{copy_id}/content", content=PAYLOAD + b"-v2", headers=ALICE)
    versions = client.get(f"/api/v1/nodes/{copy_id}/versions", headers=ALICE).json()["items"]
    first = next(v for v in versions if v["sequence"] == 1)
    restored = client.post(f"/api/v1/nodes/{copy_id}/versions/{first['id']}/restore", headers=ALICE)
    assert restored.status_code == HTTPStatus.OK, restored.text

    assert client.get(f"/api/v1/nodes/{copy_id}/content", headers=ALICE).content == PAYLOAD


def test_the_object_in_minio_holds_no_plaintext(client: TestClient) -> None:
    """`content-encryption/spec.md`: reading the object directly yields nothing.

    Checked against the real bucket, not a fake -- this is the property the
    whole capability exists for.
    """
    root = root_id(client, ALICE)
    marker = b"QUARTERLY-REVENUE-CONFIDENTIAL"
    body = marker * 40
    created = client.put(
        f"/api/v1/nodes/{root}/files/secret.bin?encrypted=true", content=body, headers=ALICE
    ).json()

    versions = client.get(f"/api/v1/nodes/{created['id']}/versions", headers=ALICE).json()
    stored = stored_object(client, str(created["id"]), str(versions["items"][0]["id"]))

    assert marker not in stored
    assert body[:64] not in stored
    assert len(stored) > len(body), "ciphertext carries a header and per-frame tags"


def stored_object(client: TestClient, node_id: str, version_id: str) -> bytes:
    """Fetch an object straight from MinIO, bypassing the API entirely.

    Owner ids are deliberately not exposed by the API, so the key is found by
    listing rather than constructed.
    """
    settings = client.app.state.settings  # type: ignore[attr-defined]
    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key.get_secret_value(),
        secret_key=settings.minio_secret_key.get_secret_value(),
        secure=False,
    )
    suffix = f"/{node_id}/{version_id}"
    for entry in minio.list_objects(settings.minio_bucket, recursive=True):
        name = entry.object_name or ""
        if name.endswith(suffix):
            return bytes(minio.get_object(settings.minio_bucket, name).read())
    raise AssertionError(f"no stored object for {suffix}")


def test_a_range_read_of_encrypted_content(client: TestClient) -> None:
    root = root_id(client, ALICE)
    created = client.put(
        f"/api/v1/nodes/{root}/files/secret.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    ).json()

    response = client.get(
        f"/api/v1/nodes/{created['id']}/content", headers={**ALICE, "Range": "bytes=50-99"}
    )
    assert response.status_code == HTTPStatus.PARTIAL_CONTENT
    assert response.content == PAYLOAD[50:100]


def test_a_plaintext_file_can_be_encrypted_in_place(client: TestClient) -> None:
    root = root_id(client, ALICE)
    created = upload(client, ALICE, root, "convert.bin", PAYLOAD)

    response = client.put(
        f"/api/v1/nodes/{created['id']}/encryption", json={"encrypted": True}, headers=ALICE
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["encrypted"] is True

    fetched = client.get(f"/api/v1/nodes/{created['id']}/content", headers=ALICE)
    assert fetched.content == PAYLOAD


def test_an_encrypted_file_can_be_decrypted(client: TestClient) -> None:
    root = root_id(client, ALICE)
    created = client.put(
        f"/api/v1/nodes/{root}/files/secret.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    ).json()

    response = client.put(
        f"/api/v1/nodes/{created['id']}/encryption", json={"encrypted": False}, headers=ALICE
    )
    assert response.status_code == HTTPStatus.OK
    assert client.get(f"/api/v1/nodes/{created['id']}/content", headers=ALICE).content == PAYLOAD


def test_an_encrypted_file_can_be_shared_and_read(client: TestClient) -> None:
    root_id(client, BOB)
    root = root_id(client, ALICE)
    created = client.put(
        f"/api/v1/nodes/{root}/files/secret.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    ).json()

    granted = client.put(
        f"/api/v1/nodes/{created['id']}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED

    assert client.get(f"/api/v1/nodes/{created['id']}/content", headers=BOB).content == PAYLOAD


def test_revoking_leaves_the_recipient_unable_to_read(client: TestClient) -> None:
    root_id(client, BOB)
    root = root_id(client, ALICE)
    created = client.put(
        f"/api/v1/nodes/{root}/files/secret.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    ).json()
    client.put(
        f"/api/v1/nodes/{created['id']}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert client.get(f"/api/v1/nodes/{created['id']}/content", headers=BOB).status_code == 200

    client.delete(f"/api/v1/nodes/{created['id']}/grants/bob", headers=ALICE)

    after = client.get(f"/api/v1/nodes/{created['id']}/content", headers=BOB)
    assert after.status_code == HTTPStatus.NOT_FOUND


def test_no_response_ever_carries_key_material(client: TestClient) -> None:
    root = root_id(client, ALICE)
    created = client.put(
        f"/api/v1/nodes/{root}/files/secret.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    ).json()

    for response in (
        client.get(f"/api/v1/nodes/{created['id']}", headers=ALICE),
        client.get(f"/api/v1/nodes/{created['id']}/versions", headers=ALICE),
        client.get(f"/api/v1/nodes/{root}/children", headers=ALICE),
    ):
        body = response.text.lower()
        for marker in ("wrapped", "dek", "kek", "master_key", "wrapped_dek"):
            assert marker not in body, f"{marker} leaked into {response.url}"
