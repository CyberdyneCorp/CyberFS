"""The main filesystem features, exercised against a deployed CyberFS.

Scope: the things only a real deployment can confirm -- that migrations ran,
that the object store is reachable and configured, that `MASTER_KEY` decrypts
what it encrypted, and that the ingress passes bodies and headers through
intact. Behavioural edge cases stay in the unit and in-process suites, which run
without a network.
"""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import httpx
import pytest

from tests.e2e.conftest import requires_deployment, upload

pytestmark = [pytest.mark.e2e, requires_deployment]

#: Larger than the default `ENCRYPTION_FRAME_BYTES` (64 KiB) so an encrypted
#: round trip crosses frame boundaries rather than fitting in one.
LARGE = 150 * 1024


# --- reachability -----------------------------------------------------------


def test_liveness_is_independent_of_dependencies(anonymous: httpx.Client) -> None:
    response = anonymous.get("/health/live")
    assert response.status_code == 200


def test_readiness_reports_every_required_component_up(anonymous: httpx.Client) -> None:
    response = anonymous.get("/health/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"

    required = {
        c["name"]: c["status"] for c in body["components"] if c["criticality"] == "required"
    }
    down = {name: status for name, status in required.items() if status != "up"}
    assert not down, f"required components not up: {down}"
    # The ones a misconfigured deployment gets wrong, named explicitly so a
    # regression says which.
    assert required.get("postgres") == "up"
    assert required.get("minio") == "up"
    assert required.get("encryption") == "up"
    assert required.get("cyberdyne_auth") == "up"


def test_unauthenticated_requests_are_refused(anonymous: httpx.Client) -> None:
    response = anonymous.get("/api/v1/nodes/root")
    assert response.status_code in (401, 403), response.text


def test_a_garbage_token_is_refused(anonymous: httpx.Client) -> None:
    response = anonymous.get(
        "/api/v1/nodes/root", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code in (401, 403), response.text


# --- provisioning -----------------------------------------------------------


def test_the_caller_has_a_stable_root(api: httpx.Client) -> None:
    first = api.get("/api/v1/nodes/root")
    second = api.get("/api/v1/nodes/root")
    assert first.status_code == 200, first.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["kind"] == "folder"


# --- folders ----------------------------------------------------------------


def test_a_folder_is_created_and_appears_in_its_parent(api: httpx.Client, scratch: str) -> None:
    name = f"folder-{uuid4().hex[:8]}"
    created = api.post(f"/api/v1/nodes/{scratch}/folders", json={"name": name})
    assert created.status_code == 201, created.text

    listing = api.get(f"/api/v1/nodes/{scratch}/children")
    assert listing.status_code == 200, listing.text
    assert name in [item["name"] for item in listing.json()["items"]]


def test_a_duplicate_folder_name_is_refused(api: httpx.Client, folder: str) -> None:
    name = f"twice-{uuid4().hex[:8]}"
    first = api.post(f"/api/v1/nodes/{folder}/folders", json={"name": name})
    assert first.status_code == 201, first.text

    second = api.post(f"/api/v1/nodes/{folder}/folders", json={"name": name})
    assert second.status_code == 409, second.text


def test_a_folder_is_renamed(api: httpx.Client, folder: str) -> None:
    renamed = f"renamed-{uuid4().hex[:8]}"
    response = api.patch(f"/api/v1/nodes/{folder}/name", json={"name": renamed})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == renamed

    assert api.get(f"/api/v1/nodes/{folder}").json()["name"] == renamed


def test_a_trashed_folder_is_gone_from_listings_and_comes_back(
    api: httpx.Client, scratch: str
) -> None:
    name = f"doomed-{uuid4().hex[:8]}"
    node_id = api.post(f"/api/v1/nodes/{scratch}/folders", json={"name": name}).json()["id"]

    assert api.delete(f"/api/v1/nodes/{node_id}").status_code in (200, 204)
    names = [i["name"] for i in api.get(f"/api/v1/nodes/{scratch}/children").json()["items"]]
    assert name not in names

    assert api.post(f"/api/v1/nodes/{node_id}/restore").status_code in (200, 201, 204)
    names = [i["name"] for i in api.get(f"/api/v1/nodes/{scratch}/children").json()["items"]]
    assert name in names


# --- files and content ------------------------------------------------------


def test_a_file_round_trips_byte_for_byte(api: httpx.Client, folder: str) -> None:
    body = os.urandom(LARGE)
    summary = upload(api, folder, "plain.bin", body)
    assert summary["size_bytes"] == len(body)

    fetched = api.get(f"/api/v1/nodes/{summary['id']}/content")
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == body


def test_overwriting_content_keeps_the_old_bytes_as_a_version(
    api: httpx.Client, folder: str
) -> None:
    first = os.urandom(4096)
    second = os.urandom(8192)

    summary = upload(api, folder, "versioned.bin", first)
    node_id = summary["id"]

    updated = api.put(
        f"/api/v1/nodes/{node_id}/content",
        content=second,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert updated.status_code == 200, updated.text
    assert api.get(f"/api/v1/nodes/{node_id}/content").content == second

    versions = api.get(f"/api/v1/nodes/{node_id}/versions")
    assert versions.status_code == 200, versions.text
    items = versions.json()["items"]
    assert len(items) >= 2, items


def test_a_previous_version_is_restored(api: httpx.Client, folder: str) -> None:
    original = os.urandom(4096)
    replacement = os.urandom(4096)

    node_id = upload(api, folder, "restore-me.bin", original)["id"]
    api.put(
        f"/api/v1/nodes/{node_id}/content",
        content=replacement,
        headers={"Content-Type": "application/octet-stream"},
    )

    items = api.get(f"/api/v1/nodes/{node_id}/versions").json()["items"]
    # Restore the version holding the original bytes: the oldest by creation.
    oldest = sorted(items, key=lambda v: v["created_at"])[0]
    restored = api.post(f"/api/v1/nodes/{node_id}/versions/{oldest['id']}/restore")
    assert restored.status_code in (200, 201), restored.text

    assert api.get(f"/api/v1/nodes/{node_id}/content").content == original


def test_a_byte_range_is_served(api: httpx.Client, folder: str) -> None:
    body = os.urandom(4096)
    node_id = upload(api, folder, "ranged.bin", body)["id"]

    response = api.get(f"/api/v1/nodes/{node_id}/content", headers={"Range": "bytes=100-199"})
    assert response.status_code == 206, response.text
    assert response.headers["content-range"] == f"bytes 100-199/{len(body)}"
    assert response.content == body[100:200]


# --- encryption -------------------------------------------------------------


def test_content_uploaded_encrypted_reads_back_identically(api: httpx.Client, folder: str) -> None:
    """The MASTER_KEY round trip, over the wire, across frame boundaries.

    If the deployment's key were wrong or its framing broken, this is where it
    shows: the upload succeeds and the download returns something else.
    """
    body = os.urandom(LARGE)
    summary = upload(api, folder, "secret.bin", body, encrypted=True)
    assert summary["encrypted"] is True

    fetched = api.get(f"/api/v1/nodes/{summary['id']}/content")
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == body


def test_encrypting_an_existing_file_preserves_its_content(api: httpx.Client, folder: str) -> None:
    body = os.urandom(LARGE)
    node_id = upload(api, folder, "to-encrypt.bin", body, encrypted=False)["id"]

    toggled = api.put(f"/api/v1/nodes/{node_id}/encryption", json={"encrypted": True})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["encrypted"] is True

    assert api.get(f"/api/v1/nodes/{node_id}/content").content == body


def test_decrypting_an_existing_file_preserves_its_content(api: httpx.Client, folder: str) -> None:
    body = os.urandom(64 * 1024 + 7)
    node_id = upload(api, folder, "to-decrypt.bin", body, encrypted=True)["id"]

    toggled = api.put(f"/api/v1/nodes/{node_id}/encryption", json={"encrypted": False})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["encrypted"] is False

    assert api.get(f"/api/v1/nodes/{node_id}/content").content == body


def test_a_folder_default_makes_new_children_encrypted(api: httpx.Client, scratch: str) -> None:
    created = api.post(
        f"/api/v1/nodes/{scratch}/folders",
        json={"name": f"encrypted-by-default-{uuid4().hex[:8]}", "encryption_default": "on"},
    )
    assert created.status_code == 201, created.text
    folder_id = created.json()["id"]

    body = os.urandom(2048)
    summary = upload(api, folder_id, "inherits.bin", body)
    assert summary["encrypted"] is True
    assert api.get(f"/api/v1/nodes/{summary['id']}/content").content == body


# --- copy and move ----------------------------------------------------------


def test_a_file_is_copied_with_its_content(api: httpx.Client, folder: str) -> None:
    body = os.urandom(4096)
    node_id = upload(api, folder, "original.bin", body)["id"]
    destination = api.post(
        f"/api/v1/nodes/{folder}/folders", json={"name": f"dest-{uuid4().hex[:8]}"}
    ).json()["id"]

    copied = api.post(f"/api/v1/nodes/{node_id}/copy", json={"parent_id": destination})
    assert copied.status_code in (200, 201), copied.text
    copy_id = copied.json()["id"]

    assert copy_id != node_id
    assert api.get(f"/api/v1/nodes/{copy_id}/content").content == body
    # The original survives a copy.
    assert api.get(f"/api/v1/nodes/{node_id}/content").content == body


def test_a_file_is_moved_between_folders(api: httpx.Client, folder: str) -> None:
    body = os.urandom(2048)
    node_id = upload(api, folder, "mover.bin", body)["id"]
    destination = api.post(
        f"/api/v1/nodes/{folder}/folders", json={"name": f"moved-to-{uuid4().hex[:8]}"}
    ).json()["id"]

    moved = api.patch(f"/api/v1/nodes/{node_id}/parent", json={"parent_id": destination})
    assert moved.status_code == 200, moved.text

    children = [i["id"] for i in api.get(f"/api/v1/nodes/{destination}/children").json()["items"]]
    assert node_id in children
    assert api.get(f"/api/v1/nodes/{node_id}/content").content == body


# --- sharing ----------------------------------------------------------------


def test_a_public_link_serves_content_without_a_token(
    api: httpx.Client, anonymous: httpx.Client, folder: str
) -> None:
    body = os.urandom(4096)
    node_id = upload(api, folder, "shared.bin", body)["id"]

    issued = api.post(f"/api/v1/nodes/{node_id}/links", json={})
    assert issued.status_code == 201, issued.text
    token = issued.json()["token"]

    metadata = anonymous.get(f"/api/v1/public/{token}")
    assert metadata.status_code == 200, metadata.text

    content = anonymous.get(f"/api/v1/public/{token}/content")
    assert content.status_code == 200, content.text
    assert content.content == body


def test_a_revoked_link_stops_serving(
    api: httpx.Client, anonymous: httpx.Client, folder: str
) -> None:
    node_id = upload(api, folder, "revoked.bin", os.urandom(1024))["id"]
    issued = api.post(f"/api/v1/nodes/{node_id}/links", json={}).json()

    assert api.delete(f"/api/v1/links/{issued['id']}").status_code in (200, 204)

    after = anonymous.get(f"/api/v1/public/{issued['token']}/content")
    assert after.status_code in (401, 403, 404, 410), after.text


def test_an_unknown_link_token_is_not_found(anonymous: httpx.Client) -> None:
    response = anonymous.get(f"/api/v1/public/{uuid4().hex}")
    assert response.status_code in (401, 403, 404), response.text


# --- discovery --------------------------------------------------------------


def test_a_trashed_node_is_purged_and_frees_its_quota(api: httpx.Client, folder: str) -> None:
    body = os.urandom(LARGE)
    node_id = upload(api, folder, "purge-me.bin", body)["id"]
    assert api.delete(f"/api/v1/nodes/{node_id}").status_code in (200, 204)

    purged = api.post(f"/api/v1/nodes/{node_id}/purge")
    assert purged.status_code == 200, purged.text
    assert purged.json()["bytes_reclaimed"] == len(body)

    # Gone for good: not restorable, and its content is unreachable.
    assert api.post(f"/api/v1/nodes/{node_id}/restore").status_code == 404
    assert api.get(f"/api/v1/nodes/{node_id}/content").status_code == 404


def test_a_live_node_cannot_be_purged(api: httpx.Client, folder: str) -> None:
    body = os.urandom(2048)
    node_id = upload(api, folder, "still-live.bin", body)["id"]

    refused = api.post(f"/api/v1/nodes/{node_id}/purge")
    assert refused.status_code == 409, refused.text
    # Untouched, and still byte-identical.
    assert api.get(f"/api/v1/nodes/{node_id}/content").content == body


def test_purging_a_folder_destroys_its_subtree(api: httpx.Client, scratch: str) -> None:
    outer = api.post(
        f"/api/v1/nodes/{scratch}/folders", json={"name": f"purge-tree-{uuid4().hex[:8]}"}
    ).json()["id"]
    inner = api.post(f"/api/v1/nodes/{outer}/folders", json={"name": "inner"}).json()["id"]
    deep = upload(api, inner, "deep.bin", os.urandom(4096))["id"]

    assert api.delete(f"/api/v1/nodes/{outer}").status_code in (200, 204)
    purged = api.post(f"/api/v1/nodes/{outer}/purge")
    assert purged.status_code == 200, purged.text
    # Exact, not a lower bound: an undercount here means a descendant's row was
    # cascaded away before its object was deleted, stranding it in the store.
    assert purged.json()["purged"] == 3, purged.text
    assert purged.json()["objects_deleted"] == 1

    for node_id in (outer, inner, deep):
        assert api.get(f"/api/v1/nodes/{node_id}").status_code == 404


def test_purging_an_unknown_node_is_not_found(api: httpx.Client) -> None:
    assert api.post(f"/api/v1/nodes/{uuid4()}/purge").status_code == 404


# --- trash ------------------------------------------------------------------
#
# The success path of `POST /api/v1/trash/purge` is deliberately NOT exercised
# here. This tier runs against a real account whose trash may hold data no test
# created and no test can put back; emptying it would destroy that data, and the
# count guard turns any concurrent deletion into a `409` that fails the session.
# Only the guard's refusal is asserted, which destroys nothing. Whole-trash
# emptying is proven in `tests/integration/test_api_trash.py`, where the database
# is disposable. Every cleanup below is `POST /api/v1/nodes/{id}/purge` on an id
# this test created.


def find_entry(api: httpx.Client, node_id: str) -> dict:
    """The caller's trash entry for `node_id`, following the cursor if needed.

    A deployed account may hold trash this suite did not create, so the entry is
    searched for rather than assumed to head the first page.
    """
    cursor: str | None = None
    for _ in range(20):
        params = {"limit": 100} if cursor is None else {"limit": 100, "cursor": cursor}
        page = api.get("/api/v1/trash", params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        for item in body["items"]:
            if item["id"] == node_id:
                return dict(item)
        cursor = body["next_cursor"]
        if cursor is None:
            break
    raise AssertionError(f"no trash entry for {node_id}")


def test_a_deleted_file_is_found_in_the_trash_and_restored(api: httpx.Client, folder: str) -> None:
    """The loop the deployment could not previously complete.

    Nothing but the assertion remembers the id across the delete: the restore is
    driven by what `GET /api/v1/trash` returned.
    """
    body = os.urandom(4096)
    node_id = upload(api, folder, "findable.bin", body)["id"]
    assert api.delete(f"/api/v1/nodes/{node_id}").status_code in (200, 204)

    entry = find_entry(api, str(node_id))
    assert entry["name"] == "findable.bin"
    assert entry["size_bytes"] == len(body)
    assert entry["node_count"] == 1
    assert entry["purge_after"] > entry["deleted_at"]

    restored = api.post(f"/api/v1/nodes/{entry['id']}/restore")
    assert restored.status_code in (200, 201), restored.text
    assert api.get(f"/api/v1/nodes/{node_id}/content").content == body

    # Leave nothing behind: trash it again and destroy it outright.
    api.delete(f"/api/v1/nodes/{node_id}")
    assert api.post(f"/api/v1/nodes/{node_id}/purge").status_code == 200


def test_a_deleted_tree_is_one_entry_and_a_stale_count_destroys_nothing(
    api: httpx.Client, scratch: str
) -> None:
    """One entry for the whole tree, and a wrong count refuses the operation.

    Only the refusal, never the success: a stated count of 10,000 cannot match any
    real trash, so this exercises the guard while destroying nothing. Cleanup is by
    id. Replacing that with an empty-trash call would destroy unrelated trash in a
    live account and make teardown depend on the feature under test.
    """
    outer = api.post(
        f"/api/v1/nodes/{scratch}/folders", json={"name": f"trash-tree-{uuid4().hex[:8]}"}
    ).json()["id"]
    inner = api.post(f"/api/v1/nodes/{outer}/folders", json={"name": "inner"}).json()["id"]
    body = os.urandom(4096)
    upload(api, inner, "deep.bin", body)
    assert api.delete(f"/api/v1/nodes/{outer}").status_code in (200, 204)

    listing = api.get("/api/v1/trash", params={"limit": 100})
    assert listing.status_code == 200, listing.text
    assert listing.json()["total_entries"] >= 1
    entry = find_entry(api, str(outer))
    assert entry["node_count"] == 3, entry
    assert entry["size_bytes"] == len(body), entry

    refused = api.post("/api/v1/trash/purge", json={"expected_entries": 10_000})
    assert refused.status_code == 409, refused.text
    assert refused.json().get("code") == "trash_count_mismatch", refused.text
    assert find_entry(api, str(outer))["id"] == str(outer), "the trash was touched anyway"

    # Destroy exactly what this test created, by id, and nothing else.
    assert api.post(f"/api/v1/nodes/{outer}/purge").status_code == 200
    assert api.get(f"/api/v1/nodes/{inner}").status_code == 404


# --- labels -----------------------------------------------------------------


def test_a_file_is_tagged_and_found_by_tag(api: httpx.Client, folder: str) -> None:
    body = os.urandom(4096)
    node_id = upload(api, folder, f"tagged-{uuid4().hex[:8]}.bin", body)["id"]
    label = f"e2e-{uuid4().hex[:8]}"

    tagged = api.put(f"/api/v1/nodes/{node_id}/tags", json={"tags": [label.upper()]})
    assert tagged.status_code == 200, tagged.text
    assert tagged.json()["tags"] == [label], "stored normalized"

    found = api.get("/api/v1/search", params={"tag": label})
    assert found.status_code == 200, found.text
    assert node_id in [item["id"] for item in found.json()["items"]]


def test_metadata_is_set_and_searched(api: httpx.Client, folder: str) -> None:
    node_id = upload(api, folder, f"annotated-{uuid4().hex[:8]}.bin", os.urandom(512))["id"]
    value = uuid4().hex[:12]

    annotated = api.put(
        f"/api/v1/nodes/{node_id}/metadata",
        json={"metadata": [{"key": "e2e-source", "value": value}]},
    )
    assert annotated.status_code == 200, annotated.text
    assert annotated.json()["metadata"]["e2e-source"] == value

    by_key = api.get("/api/v1/search", params={"key": "e2e-source", "value": value})
    assert by_key.status_code == 200, by_key.text
    assert [item["id"] for item in by_key.json()["items"]] == [node_id]


def test_a_tag_is_added_and_removed_by_patch(api: httpx.Client, folder: str) -> None:
    """The round trip a partial update exists to make unnecessary, end to end."""
    node_id = upload(api, folder, f"patched-{uuid4().hex[:8]}.bin", os.urandom(1024))["id"]
    label = f"e2e-patch-{uuid4().hex[:8]}"

    added = api.patch(f"/api/v1/nodes/{node_id}/tags", json={"add": [label.upper()]})
    assert added.status_code == 200, added.text
    assert label in added.json()["tags"], "stored normalized"

    found = api.get("/api/v1/search", params={"tag": label})
    assert found.status_code == 200, found.text
    assert node_id in [item["id"] for item in found.json()["items"]]

    removed = api.patch(f"/api/v1/nodes/{node_id}/tags", json={"remove": [label]})
    assert removed.status_code == 200, removed.text
    assert label not in removed.json()["tags"]
    assert node_id not in [
        item["id"] for item in api.get("/api/v1/search", params={"tag": label}).json()["items"]
    ]


def test_two_metadata_patches_contribute_without_reading_first(
    api: httpx.Client, folder: str
) -> None:
    """Neither request names the other's key, and both keys survive."""
    node_id = upload(api, folder, f"contributed-{uuid4().hex[:8]}.bin", os.urandom(512))["id"]

    first = api.patch(
        f"/api/v1/nodes/{node_id}/metadata",
        json={"set": [{"key": "e2e-first", "value": "1"}]},
    )
    assert first.status_code == 200, first.text
    second = api.patch(
        f"/api/v1/nodes/{node_id}/metadata",
        json={"set": [{"key": "e2e-second", "value": "2"}]},
    )
    assert second.status_code == 200, second.text

    assert second.json()["metadata"]["e2e-first"] == "1"
    assert second.json()["metadata"]["e2e-second"] == "2"


def test_a_repeated_identical_patch_returns_the_same_etag(api: httpx.Client, folder: str) -> None:
    """A patch that changes nothing is not a write, so the validator must not move."""
    node_id = upload(api, folder, f"stable-{uuid4().hex[:8]}.bin", os.urandom(512))["id"]
    label = f"e2e-stable-{uuid4().hex[:8]}"

    first = api.patch(f"/api/v1/nodes/{node_id}/tags", json={"add": [label]})
    assert first.status_code == 200, first.text
    second = api.patch(f"/api/v1/nodes/{node_id}/tags", json={"add": [label]})
    assert second.status_code == 200, second.text

    etag = first.headers["ETag"]
    assert second.headers["ETag"] == etag
    # And it is a usable validator, not just a stable string: the ingress must
    # pass `If-Match` through for a patch as it does for a rename.
    conditional = api.patch(
        f"/api/v1/nodes/{node_id}/tags",
        json={"add": [f"{label}-next"]},
        headers={"If-Match": etag},
    )
    assert conditional.status_code == 200, conditional.text


def test_the_digest_matches_the_bytes_uploaded(api: httpx.Client, folder: str) -> None:
    """Across frame boundaries and encrypted, where a broken digest would show."""
    body = os.urandom(LARGE)
    expected = hashlib.sha256(body).hexdigest()
    node_id = upload(api, folder, "digested.bin", body, encrypted=True)["id"]

    assert api.get(f"/api/v1/nodes/{node_id}").json()["digest"] == expected
    versions = api.get(f"/api/v1/nodes/{node_id}/versions").json()["items"]
    assert versions[0]["digest"] == expected


def test_search_finds_a_file_by_name(api: httpx.Client, folder: str) -> None:
    name = f"findable-{uuid4().hex[:8]}.bin"
    node_id = upload(api, folder, name, os.urandom(512))["id"]

    response = api.get("/api/v1/search", params={"q": name})
    assert response.status_code == 200, response.text
    assert node_id in [item["id"] for item in response.json()["items"]]


def test_a_search_larger_than_one_page_is_walked_to_exhaustion(
    api: httpx.Client, folder: str
) -> None:
    """Against the real ingress, which is where a dropped query parameter shows."""
    term = f"paged{uuid4().hex[:8]}"
    created = {
        str(api.post(f"/api/v1/nodes/{folder}/folders", json={"name": f"{term}-{i}"}).json()["id"])
        for i in range(5)
    }
    assert len(created) == 5

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"q": term, "limit": 2} | ({"cursor": cursor} if cursor else {})
        response = api.get("/api/v1/search", params=params)
        assert response.status_code == 200, response.text
        collected.extend(item["id"] for item in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    else:  # pragma: no cover - a cursor that never clears is a failure, not a loop
        pytest.fail("the cursor never cleared")

    assert set(collected) == created
    assert len(collected) == len(created), "a page boundary dropped or repeated a node"

    for node_id in created:
        api.delete(f"/api/v1/nodes/{node_id}")
        api.post(f"/api/v1/nodes/{node_id}/purge")


def test_the_tag_inventory_agrees_with_the_paginated_tag_search(
    api: httpx.Client, folder: str
) -> None:
    label = f"e2etag{uuid4().hex[:8]}"
    created = [
        str(api.post(f"/api/v1/nodes/{folder}/folders", json={"name": f"{label}-{i}"}).json()["id"])
        for i in range(3)
    ]
    for node_id in created:
        tagged = api.put(f"/api/v1/nodes/{node_id}/tags", json={"tags": [label]})
        assert tagged.status_code == 200, tagged.text

    inventory = api.get("/api/v1/tags", params={"prefix": label})
    assert inventory.status_code == 200, inventory.text
    assert [(i["tag"], i["count"]) for i in inventory.json()["items"]] == [(label, len(created))]

    walked: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"tag": label, "limit": 1} | ({"cursor": cursor} if cursor else {})
        response = api.get("/api/v1/search", params=params)
        assert response.status_code == 200, response.text
        walked.extend(item["id"] for item in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert sorted(walked) == sorted(created), "the count promised what the search returns"

    for node_id in created:
        api.delete(f"/api/v1/nodes/{node_id}")
        api.post(f"/api/v1/nodes/{node_id}/purge")


def test_activity_records_what_the_caller_did(api: httpx.Client, folder: str) -> None:
    upload(api, folder, f"audited-{uuid4().hex[:8]}.bin", os.urandom(512))

    response = api.get("/api/v1/me/activity")
    assert response.status_code == 200, response.text
    assert response.json()["items"], "no activity recorded for a caller that just wrote a file"
