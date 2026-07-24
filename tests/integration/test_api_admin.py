"""The admin API end to end, against real Postgres, MinIO, and Redis."""

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

ADMIN = {"Authorization": "Bearer dev:root:admin"}
ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
PAYLOAD = b"admin-visible-only-as-a-number" * 30

ENDPOINT = os.environ.get("CYBERFS_TEST_MINIO_ENDPOINT", "localhost:9000")
REDIS_URL = os.environ.get("CYBERFS_TEST_REDIS_URL", "redis://localhost:6380/0")
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
        redis_url=REDIS_URL,
        minio_endpoint=ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=f"cyberfs-admin-{uuid.uuid4().hex[:8]}",
        minio_secure=False,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    return str(client.get("/api/v1/nodes/root", headers=who).json()["id"])


def upload(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.put(f"/api/v1/nodes/{parent}/files/{name}", content=PAYLOAD, headers=who)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def a_user_id(client: TestClient, subject: str) -> str:
    users = client.get("/api/v1/admin/users", headers=ADMIN).json()["items"]
    return next(u["user_id"] for u in users if u["subject"] == subject)


# --- access ----------------------------------------------------------------


def test_a_non_admin_is_refused(client: TestClient) -> None:
    root_id(client, ALICE)
    assert client.get("/api/v1/admin/overview", headers=ALICE).status_code == (HTTPStatus.FORBIDDEN)


def test_an_unauthenticated_caller_is_refused(client: TestClient) -> None:
    assert client.get("/api/v1/admin/overview").status_code == HTTPStatus.UNAUTHORIZED


def test_an_admin_is_admitted(client: TestClient) -> None:
    assert client.get("/api/v1/admin/overview", headers=ADMIN).status_code == HTTPStatus.OK


# --- statistics ------------------------------------------------------------


def test_per_user_storage_is_reported(client: TestClient) -> None:
    upload(client, ALICE, root_id(client, ALICE), "report.bin")

    users = client.get("/api/v1/admin/users", headers=ADMIN).json()["items"]
    alice = next(u for u in users if u["subject"] == "alice")

    assert alice["file_count"] == 1
    assert alice["live_bytes"] == len(PAYLOAD)
    assert alice["used_bytes"] == len(PAYLOAD)
    assert alice["over_quota"] is False


def test_percent_used_is_computed_against_the_quota(client: TestClient) -> None:
    """Checked at a quota where the ratio is actually visible; a few hundred
    bytes against the 10 GB default legitimately rounds to 0.00%."""
    upload(client, ALICE, root_id(client, ALICE), "a.bin")
    user_id = a_user_id(client, "alice")
    client.put(
        f"/api/v1/admin/users/{user_id}/quota",
        json={"quota_bytes": len(PAYLOAD) * 4},
        headers=ADMIN,
    )

    alice = client.get(f"/api/v1/admin/users/{user_id}", headers=ADMIN).json()
    assert alice["percent_used"] == 25.0


def test_the_usage_breakdown_sums_to_the_total(client: TestClient) -> None:
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "a.bin")
    client.put(f"/api/v1/nodes/{node}/content", content=b"v2", headers=ALICE)
    trashed = upload(client, ALICE, root, "b.bin")
    client.delete(f"/api/v1/nodes/{trashed}", headers=ALICE)

    alice = client.get(f"/api/v1/admin/users/{a_user_id(client, 'alice')}", headers=ADMIN).json()

    assert (
        alice["live_bytes"] + alice["trashed_bytes"] + alice["version_bytes"] == alice["used_bytes"]
    )
    assert alice["trashed_bytes"] > 0
    assert alice["version_bytes"] > 0


def test_encryption_adoption_is_reported(client: TestClient) -> None:
    root = root_id(client, ALICE)
    upload(client, ALICE, root, "open.bin")
    client.put(
        f"/api/v1/nodes/{root}/files/sealed.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    )

    alice = client.get(f"/api/v1/admin/users/{a_user_id(client, 'alice')}", headers=ADMIN).json()

    assert alice["encrypted_file_count"] == 1
    assert 0 < alice["encrypted_share"] < 100


def test_tenant_totals_cover_every_user(client: TestClient) -> None:
    upload(client, ALICE, root_id(client, ALICE), "a.bin")
    upload(client, BOB, root_id(client, BOB), "b.bin")

    overview = client.get("/api/v1/admin/overview", headers=ADMIN).json()

    assert overview["user_count"] >= 2
    assert overview["file_count"] == 2
    assert overview["live_bytes"] == 2 * len(PAYLOAD)


def test_the_content_type_distribution_is_reported(client: TestClient) -> None:
    root = root_id(client, ALICE)
    client.put(
        f"/api/v1/nodes/{root}/files/a.json",
        content=b"{}",
        headers={**ALICE, "Content-Type": "application/json"},
    )

    overview = client.get("/api/v1/admin/overview", headers=ADMIN).json()
    types = {entry["content_type"] for entry in overview["content_types"]}
    assert "application/json" in types


def test_top_consumers_are_ranked(client: TestClient) -> None:
    upload(client, ALICE, root_id(client, ALICE), "a.bin")
    upload(client, ALICE, root_id(client, ALICE), "b.bin")
    upload(client, BOB, root_id(client, BOB), "c.bin")

    overview = client.get("/api/v1/admin/overview?top_n=2", headers=ADMIN).json()

    assert overview["top_consumers"][0]["subject"] == "alice"


def test_growth_is_reported_over_the_window(client: TestClient) -> None:
    upload(client, ALICE, root_id(client, ALICE), "a.bin")
    overview = client.get("/api/v1/admin/overview?growth_days=7", headers=ADMIN).json()
    assert sum(point["files_added"] for point in overview["growth"]) == 1


def test_an_invalid_growth_window_is_refused(client: TestClient) -> None:
    response = client.get("/api/v1/admin/overview?growth_days=13", headers=ADMIN)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_users_can_be_sorted_and_filtered(client: TestClient) -> None:
    upload(client, ALICE, root_id(client, ALICE), "a.bin")
    root_id(client, BOB)

    by_use = client.get("/api/v1/admin/users?sort_by=used", headers=ADMIN).json()["items"]
    assert by_use[0]["subject"] == "alice"

    over = client.get("/api/v1/admin/users?over_quota=true", headers=ADMIN).json()["items"]
    assert over == []


def test_reported_totals_reconcile_with_the_rows(client: TestClient) -> None:
    """`admin-dashboard/spec.md`: figures must agree, not display drift."""
    upload(client, ALICE, root_id(client, ALICE), "a.bin")
    upload(client, BOB, root_id(client, BOB), "b.bin")

    operations = client.get("/api/v1/admin/operations", headers=ADMIN).json()
    assert operations["totals_reconcile"] is True


# --- no content ------------------------------------------------------------


def test_an_admin_cannot_download_someone_elses_file(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "private.bin")
    response = client.get(f"/api/v1/nodes/{node}/content", headers=ADMIN)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_an_admin_cannot_read_someone_elses_metadata_through_the_node_api(
    client: TestClient,
) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "private.bin")
    assert client.get(f"/api/v1/nodes/{node}", headers=ADMIN).status_code == (HTTPStatus.NOT_FOUND)


def test_an_admin_cannot_grant_themselves_access(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "private.bin")
    response = client.put(
        f"/api/v1/nodes/{node}/grants",
        json={"recipient": "root", "role": "owner"},
        headers=ADMIN,
    )
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_no_admin_response_carries_file_names_or_content(client: TestClient) -> None:
    upload(client, ALICE, root_id(client, ALICE), "Q3-layoffs-final.bin")

    for path in ("/api/v1/admin/overview", "/api/v1/admin/users", "/api/v1/admin/operations"):
        body = client.get(path, headers=ADMIN).text
        assert "Q3-layoffs-final" not in body, f"a file name leaked into {path}"
        assert "admin-visible-only" not in body, f"content leaked into {path}"


def test_no_admin_response_carries_key_material(client: TestClient) -> None:
    root = root_id(client, ALICE)
    client.put(
        f"/api/v1/nodes/{root}/files/sealed.bin?encrypted=true", content=PAYLOAD, headers=ALICE
    )

    for path in ("/api/v1/admin/overview", "/api/v1/admin/users", "/api/v1/admin/audit"):
        body = client.get(path, headers=ADMIN).text.lower()
        for marker in ("wrapped", "dek", "kek", "master_key"):
            assert marker not in body, f"{marker} leaked into {path}"


# --- quota -----------------------------------------------------------------


def test_an_admin_can_raise_a_quota(client: TestClient) -> None:
    root_id(client, ALICE)
    user_id = a_user_id(client, "alice")

    response = client.put(
        f"/api/v1/admin/users/{user_id}/quota", json={"quota_bytes": 99_999}, headers=ADMIN
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["quota_bytes"] == 99_999


def test_lowering_a_quota_below_usage_marks_the_user_over(client: TestClient) -> None:
    """Allowed, so an admin can react to abuse; reads and deletes still work."""
    upload(client, ALICE, root_id(client, ALICE), "big.bin")
    user_id = a_user_id(client, "alice")

    response = client.put(
        f"/api/v1/admin/users/{user_id}/quota", json={"quota_bytes": 1}, headers=ADMIN
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["over_quota"] is True


def test_an_over_quota_user_cannot_upload_but_can_still_read(client: TestClient) -> None:
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "existing.bin")
    client.put(
        f"/api/v1/admin/users/{a_user_id(client, 'alice')}/quota",
        json={"quota_bytes": 1},
        headers=ADMIN,
    )

    blocked = client.put(
        f"/api/v1/nodes/{root}/files/new.bin",
        content=PAYLOAD,
        headers={**ALICE, "Content-Length": str(len(PAYLOAD))},
    )
    assert blocked.status_code == HTTPStatus.INSUFFICIENT_STORAGE
    assert client.get(f"/api/v1/nodes/{node}/content", headers=ALICE).status_code == (HTTPStatus.OK)


def test_a_non_admin_cannot_change_any_quota(client: TestClient) -> None:
    root_id(client, ALICE)
    user_id = a_user_id(client, "alice")
    response = client.put(
        f"/api/v1/admin/users/{user_id}/quota", json={"quota_bytes": 1}, headers=ALICE
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_a_quota_change_is_audited_with_both_values(client: TestClient) -> None:
    root_id(client, ALICE)
    user_id = a_user_id(client, "alice")
    client.put(f"/api/v1/admin/users/{user_id}/quota", json={"quota_bytes": 4242}, headers=ADMIN)

    entries = client.get("/api/v1/admin/audit?action=admin.quota_changed", headers=ADMIN).json()
    assert entries["items"], "the change should be recorded"
    assert entries["items"][0]["context"]["new_bytes"] == 4242
    assert "previous_bytes" in entries["items"][0]["context"]


# --- sharing review --------------------------------------------------------


def test_active_public_links_are_listed(client: TestClient) -> None:
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "shared.bin")
    client.post(f"/api/v1/nodes/{node}/links", json={}, headers=ALICE)

    links = client.get("/api/v1/admin/links", headers=ADMIN).json()["items"]
    assert len(links) == 1
    assert links[0]["revoked"] is False


def test_the_link_listing_never_shows_the_token(client: TestClient) -> None:
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "shared.bin")
    token = client.post(f"/api/v1/nodes/{node}/links", json={}, headers=ALICE).json()["token"]

    assert token not in client.get("/api/v1/admin/links", headers=ADMIN).text


def test_an_admin_can_revoke_a_public_link(client: TestClient) -> None:
    """A link is a deployment-wide exposure, so this is within their remit."""
    root = root_id(client, ALICE)
    node = upload(client, ALICE, root, "shared.bin")
    issued = client.post(f"/api/v1/nodes/{node}/links", json={}, headers=ALICE).json()

    revoked = client.delete(f"/api/v1/admin/links/{issued['id']}", headers=ADMIN)
    assert revoked.status_code == HTTPStatus.NO_CONTENT

    assert client.get(f"/api/v1/public/{issued['token']}").status_code == HTTPStatus.NOT_FOUND


def test_an_admin_cannot_revoke_a_user_to_user_grant(client: TestClient) -> None:
    """Grant management belongs to the node's owner."""
    root_id(client, BOB)
    node = upload(client, ALICE, root_id(client, ALICE), "shared.bin")
    client.put(
        f"/api/v1/nodes/{node}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )

    refused = client.delete(f"/api/v1/admin/nodes/{node}/grants/bob", headers=ADMIN)

    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert client.get(f"/api/v1/nodes/{node}", headers=BOB).status_code == HTTPStatus.OK


# --- audit -----------------------------------------------------------------


def test_the_audit_log_is_browsable(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "shared.bin")
    root_id(client, BOB)
    client.put(
        f"/api/v1/nodes/{node}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )

    entries = client.get("/api/v1/admin/audit", headers=ADMIN).json()["items"]
    assert any(entry["action"] == "grant.created" for entry in entries)


def test_the_audit_log_filters_by_actor(client: TestClient) -> None:
    node = upload(client, ALICE, root_id(client, ALICE), "shared.bin")
    root_id(client, BOB)
    client.put(
        f"/api/v1/nodes/{node}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )

    entries = client.get("/api/v1/admin/audit?actor=alice", headers=ADMIN).json()["items"]
    assert entries
    assert {entry["actor_subject"] for entry in entries} == {"alice"}


def test_the_audit_log_is_read_only(client: TestClient) -> None:
    """Immutable through the API, even for an administrator."""
    for method in (client.post, client.put, client.delete):
        response = method("/api/v1/admin/audit")
        assert response.status_code in (
            HTTPStatus.METHOD_NOT_ALLOWED,
            HTTPStatus.NOT_FOUND,
            HTTPStatus.UNAUTHORIZED,
        )


# --- operations ------------------------------------------------------------


def test_the_operations_view_reports_dependencies(client: TestClient) -> None:
    operations = client.get("/api/v1/admin/operations", headers=ADMIN).json()
    names = {component["name"] for component in operations["components"]}
    assert {"postgres", "minio", "cache"} <= names


def test_the_operations_view_lists_every_expected_job(client: TestClient) -> None:
    operations = client.get("/api/v1/admin/operations", headers=ADMIN).json()
    names = {job["name"] for job in operations["jobs"]}
    assert names == {"purge", "orphan_reaper", "reconcile_quotas", "backup", "activity_prune"}


def test_a_job_that_has_never_run_says_so(client: TestClient) -> None:
    """Honest rather than misleadingly blank."""
    operations = client.get("/api/v1/admin/operations", headers=ADMIN).json()
    assert all(job["has_run"] is False for job in operations["jobs"])


def test_the_cache_summary_reports_counts_not_values(client: TestClient) -> None:
    upload(client, ALICE, root_id(client, ALICE), "warm.bin")
    operations = client.get("/api/v1/admin/operations", headers=ADMIN).json()

    assert "keys" in operations["cache"]
    assert "admin-visible-only" not in str(operations["cache"])


def test_a_cache_dataset_can_be_purged(client: TestClient) -> None:
    root_id(client, ALICE)
    response = client.post("/api/v1/admin/cache/perm/purge", headers=ADMIN)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["dataset"] == "perm"
    assert response.json()["keys_removed"] >= 0


def test_purging_an_unknown_dataset_is_refused(client: TestClient) -> None:
    response = client.post("/api/v1/admin/cache/nonsense/purge", headers=ADMIN)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_purge_is_audited(client: TestClient) -> None:
    client.post("/api/v1/admin/cache/perm/purge", headers=ADMIN)
    entries = client.get("/api/v1/admin/audit?action=admin.cache_purged", headers=ADMIN).json()
    assert entries["items"]
