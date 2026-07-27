"""The admin surface against a deployed CyberFS.

Ten operations with no coverage at this tier, several of which are only
meaningful against a real deployment: `/admin/operations` reports the scheduled
jobs' last runs, and `/admin/operations/backups` lists what is actually in the
backup bucket. Neither says anything in-process, where no scheduler has ever
fired and no bucket holds a dump.

The security invariant matters more than the happy paths. Admin responses
deliberately withhold node names, paths and plaintext digests: an administrator
can see that a user holds 40 GB without being able to read what it is, and a
plaintext digest would let a holder confirm which user has a specific known file
even when it is encrypted. That is asserted here against real data.
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import RUN_BACKUP, requires_deployment

pytestmark = [pytest.mark.e2e, requires_deployment]

#: Field names that must never appear in an admin response body. `digest` is the
#: load-bearing one; the others would turn the admin view into a file browser.
WITHHELD_FIELDS = ("digest", "plaintext_digest", "object_key", "content", "path")


@pytest.fixture(autouse=True)
def _needs_admin(is_admin: bool) -> None:
    if not is_admin:
        pytest.skip("the configured account is not an administrator")


def me(api: httpx.Client, subject: str) -> dict:
    """The caller's own row out of the admin user list.

    There is no `/admin/users/me`, so the row is matched on the subject taken
    from the caller's own token. Matched rather than assumed to be first: the
    listing is ordered by subject, and on a deployment with more than one account
    `items[0]` is somebody else.
    """
    listed = api.get("/api/v1/admin/users", params={"limit": 200})
    assert listed.status_code == 200, listed.text
    for row in listed.json()["items"]:
        if row.get("subject") == subject:
            return dict(row)
    pytest.skip("the calling account does not appear in the admin user list")


# --- read-only views -------------------------------------------------------


def test_the_overview_reports_totals(api: httpx.Client) -> None:
    response = api.get("/api/v1/admin/overview")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_count"] >= 1, body
    assert body["total_bytes"] >= 0, body
    # The buckets must account for the total, or the dashboard misreports storage.
    assert body["live_bytes"] + body["trashed_bytes"] <= body["total_bytes"], body


def test_the_user_list_paginates_and_withholds_content(api: httpx.Client) -> None:
    response = api.get("/api/v1/admin/users", params={"limit": 1})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) <= 1
    for field in WITHHELD_FIELDS:
        assert field not in response.text, f"the admin user list exposes {field!r}"


def test_a_user_detail_reports_usage_without_naming_a_single_file(
    api: httpx.Client, subject: str
) -> None:
    """The invariant: usage is a number, not an inventory."""
    row = me(api, subject)

    response = api.get(f"/api/v1/admin/users/{row['user_id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quota_bytes"] >= 0
    for field in WITHHELD_FIELDS:
        assert field not in response.text, f"the admin user detail exposes {field!r}"


def test_the_audit_log_is_readable_and_carries_no_digest(api: httpx.Client) -> None:
    response = api.get("/api/v1/admin/audit", params={"limit": 25})

    assert response.status_code == 200, response.text
    assert "digest" not in response.text, "an audit record must not carry a plaintext digest"


def test_the_link_register_lists_published_links(api: httpx.Client) -> None:
    response = api.get("/api/v1/admin/links", params={"limit": 25})

    assert response.status_code == 200, response.text
    assert isinstance(response.json()["items"], list)


def test_the_operations_view_reports_the_scheduled_jobs(api: httpx.Client) -> None:
    """Only meaningful against a deployment: in-process, nothing has ever run."""
    response = api.get("/api/v1/admin/operations")

    assert response.status_code == 200, response.text
    body = response.json()
    named = {job["name"] for job in body["jobs"]}
    assert {"purge", "orphan_reaper", "reconcile_quotas"} <= named, named
    # Every job reports whether it has ever run, so an operator can tell a
    # never-scheduled job from one that ran and failed.
    assert all("has_run" in job for job in body["jobs"]), body["jobs"]


def test_the_backup_list_is_readable(api: httpx.Client) -> None:
    """Reading the register needs no opt-in; running a backup does."""
    response = api.get("/api/v1/admin/operations/backups")

    assert response.status_code == 200, response.text
    assert isinstance(response.json()["items"], list)


# --- writes ----------------------------------------------------------------


def test_a_quota_is_changed_and_restored(api: httpx.Client, subject: str) -> None:
    """Round-tripped rather than left altered: this is a real account's quota.

    Restoring it in the same test rather than a fixture teardown, so a failure
    between the two is visible as a failure rather than as a silently raised
    limit on the deployment.
    """
    row = me(api, subject)
    user_id, original = row["user_id"], int(row["quota_bytes"])
    raised = original + 1024

    try:
        changed = api.put(f"/api/v1/admin/users/{user_id}/quota", json={"quota_bytes": raised})
        assert changed.status_code == 200, changed.text
        assert int(api.get(f"/api/v1/admin/users/{user_id}").json()["quota_bytes"]) == raised
    finally:
        restored = api.put(f"/api/v1/admin/users/{user_id}/quota", json={"quota_bytes": original})
        assert restored.status_code == 200, restored.text

    assert int(api.get(f"/api/v1/admin/users/{user_id}").json()["quota_bytes"]) == original


def test_a_negative_quota_is_refused(api: httpx.Client, subject: str) -> None:
    row = me(api, subject)
    response = api.put(f"/api/v1/admin/users/{row['user_id']}/quota", json={"quota_bytes": -1})
    assert response.status_code == 422, response.text


def test_purging_a_cache_dataset_is_accepted(api: httpx.Client) -> None:
    """Safe by construction: the cache is an accelerator, so an empty one is
    correct and merely slower."""
    response = api.post("/api/v1/admin/cache/perm/purge")
    assert response.status_code in (200, 202, 204), response.text


def test_an_unknown_cache_dataset_is_refused(api: httpx.Client) -> None:
    assert api.post("/api/v1/admin/cache/not-a-dataset/purge").status_code in (404, 422)


@pytest.mark.skipif(not RUN_BACKUP, reason="set CYBERFS_LIVE_RUN_BACKUP=1 to start a real backup")
def test_a_backup_can_be_triggered_and_appears_in_the_register(api: httpx.Client) -> None:
    """Opt-in: this runs `pg_dump` on the deployment and uploads the result.

    Worth having because it is the only test that proves the backup path works
    end to end -- including that `pg_dump`'s version matches the server's, which
    is the failure that made every earlier backup fail silently.
    """
    before = {row["key"] for row in api.get("/api/v1/admin/operations/backups").json()["items"]}

    started = api.post("/api/v1/admin/operations/backup", json={})
    assert started.status_code in (200, 202), started.text

    after = api.get("/api/v1/admin/operations/backups")
    assert after.status_code == 200, after.text
    assert {row["key"] for row in after.json()["items"]} - before, (
        "the backup reported success but the register gained no artifact"
    )


# --- the boundary ----------------------------------------------------------


def test_an_admin_route_is_refused_without_a_token(anonymous: httpx.Client) -> None:
    assert anonymous.get("/api/v1/admin/overview").status_code in (401, 403)


def test_an_administrator_cannot_revoke_a_grant_even_on_a_node_they_can_see(
    api: httpx.Client, folder: str
) -> None:
    """The route exists and always refuses, which is the design.

    `admin-dashboard/spec.md` forbids an administrator reaching into a private
    arrangement between two users, so `application/admin.py` raises
    `PermissionDeniedError` unconditionally. Pinned here because a route that is
    published and always answers 403 looks exactly like a bug to whoever finds it
    next -- and "fixing" it would quietly hand administrators a capability the
    spec withholds. The owner's own revoke is covered in the sharing suite.
    """
    from uuid import uuid4

    peer = str(uuid4())
    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer, "role": "viewer"})

    refused = api.delete(f"/api/v1/admin/nodes/{folder}/grants/{peer}")

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "permission_denied", refused.text
    held = api.get(f"/api/v1/nodes/{folder}/grants").json()["items"]
    assert [row["subject"] for row in held] == [peer], "the grant must be untouched"
