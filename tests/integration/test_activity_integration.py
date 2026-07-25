"""`GET /api/v1/me/activity` end to end, against real Postgres and MinIO.

A user's uploads, downloads, and shares appear in their own activity and in
nobody else's -- the privacy boundary `activity-reporting/spec.md` draws.
"""

from __future__ import annotations

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
PAYLOAD = b"activity round trip" * 16

ENDPOINT = "localhost:9000"
_unreachable: str | None = None


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    global _unreachable
    if _unreachable is not None:
        pytest.skip(_unreachable)

    bucket = f"cyberfs-activity-{uuid.uuid4().hex[:8]}"
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


def activity(client: TestClient, who: dict[str, str]) -> dict[str, object]:
    response = client.get("/api/v1/me/activity?window_days=30", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return dict(response.json())


def test_uploads_downloads_and_shares_appear_in_the_owners_activity(client: TestClient) -> None:
    root_id(client, BOB)  # provision Bob so the grant resolves
    root = root_id(client, ALICE)

    created = client.put(
        f"/api/v1/nodes/{root}/files/report.txt",
        content=PAYLOAD,
        headers={**ALICE, "Content-Type": "application/octet-stream"},
    )
    assert created.status_code == HTTPStatus.CREATED
    node_id = created.json()["id"]

    assert client.get(f"/api/v1/nodes/{node_id}/content", headers=ALICE).status_code == 200
    granted = client.put(
        f"/api/v1/nodes/{node_id}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED

    report = activity(client, ALICE)
    summary = report["summary"]
    assert summary["uploads"] >= 1  # type: ignore[index]
    assert summary["downloads"] >= 1  # type: ignore[index]
    assert summary["shares_granted"] >= 1  # type: ignore[index]

    actions = {item["action"] for item in report["items"]}  # type: ignore[union-attr]
    assert "file.uploaded" in actions
    assert "file.downloaded" in actions


def test_one_users_activity_does_not_appear_in_anothers(client: TestClient) -> None:
    root = root_id(client, ALICE)
    bob_root = root_id(client, BOB)

    created = client.put(
        f"/api/v1/nodes/{root}/files/private.txt",
        content=PAYLOAD,
        headers={**ALICE, "Content-Type": "application/octet-stream"},
    )
    node_id = created.json()["id"]
    client.get(f"/api/v1/nodes/{node_id}/content", headers=ALICE)

    report = activity(client, BOB)
    summary = report["summary"]
    # Bob did nothing but read his own (empty) root; none of Alice's counts leak.
    assert summary["uploads"] == 0  # type: ignore[index]
    assert summary["downloads"] == 0  # type: ignore[index]
    assert summary["shares_granted"] == 0  # type: ignore[index]

    node_ids = {item["node_id"] for item in report["items"]}  # type: ignore[union-attr]
    assert node_id not in node_ids
    assert bob_root is not None


def test_a_window_beyond_the_maximum_is_refused(client: TestClient) -> None:
    root_id(client, ALICE)
    response = client.get("/api/v1/me/activity?window_days=9999", headers=ALICE)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
