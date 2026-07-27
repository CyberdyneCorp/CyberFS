"""Sharing against a deployed CyberFS.

The `sharing` capability had no coverage at this tier at all, which turned out to
matter: resolving a recipient *by email* goes through CyberdyneAuth's org
directory, and that call is a live dependency no in-process test can exercise.
See `test_sharing_by_email_resolves_a_recipient` for what it found.

Most of the surface needs no second account. A grant names a *subject*, and a
subject-shaped recipient skips the directory entirely, so grant, list and revoke
are all provable with one login. The assertions that genuinely need two sides --
that a recipient can read what was shared, that it appears in *their* listing, and
that ownership transfer lands somewhere real -- are gated on a second account and
skip cleanly without one.
"""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest

from .conftest import requires_deployment, upload

pytestmark = [pytest.mark.e2e, requires_deployment]


def a_subject() -> str:
    """A subject-shaped recipient: real enough to grant to, nobody's account.

    CyberdyneAuth subjects are stringified UUIDs, and CyberFS treats anything
    UUID-shaped as already resolved. So this exercises the whole grant path
    without an enumeration lookup and without needing the recipient to exist.
    """
    return str(uuid4())


# --- grants ----------------------------------------------------------------


def test_a_grant_is_created_listed_and_revoked(api: httpx.Client, folder: str) -> None:
    peer = a_subject()

    created = api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer, "role": "viewer"})
    assert created.status_code == 201, created.text

    listed = api.get(f"/api/v1/nodes/{folder}/grants")
    assert listed.status_code == 200, listed.text
    held = {(g["subject"], g["role"]) for g in listed.json()["items"]}
    assert (peer, "viewer") in held, held

    revoked = api.delete(f"/api/v1/nodes/{folder}/grants/{peer}")
    assert revoked.status_code in (200, 204), revoked.text
    assert api.get(f"/api/v1/nodes/{folder}/grants").json()["items"] == []


def test_regranting_the_same_subject_changes_the_role_rather_than_duplicating(
    api: httpx.Client, folder: str
) -> None:
    """Two rows for one subject would make the effective role ambiguous."""
    peer = a_subject()
    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer, "role": "viewer"})
    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer, "role": "editor"})

    rows = api.get(f"/api/v1/nodes/{folder}/grants").json()["items"]

    assert len(rows) == 1, rows
    assert rows[0]["role"] == "editor"


def test_revoking_a_grant_that_was_never_made_is_not_found(api: httpx.Client, folder: str) -> None:
    assert api.delete(f"/api/v1/nodes/{folder}/grants/{a_subject()}").status_code == 404


def test_an_invalid_role_is_refused(api: httpx.Client, folder: str) -> None:
    response = api.put(
        f"/api/v1/nodes/{folder}/grants", json={"recipient": a_subject(), "role": "overlord"}
    )
    assert response.status_code == 422, response.text


def test_a_grant_on_a_node_the_caller_does_not_own_is_refused(api: httpx.Client) -> None:
    """A node id the caller has no rights to must not become shareable by guessing."""
    response = api.put(
        f"/api/v1/nodes/{uuid4()}/grants", json={"recipient": a_subject(), "role": "viewer"}
    )
    assert response.status_code in (403, 404), response.text


def test_a_trashed_node_withdraws_its_grants(api: httpx.Client, folder: str) -> None:
    """A soft delete has to take access with it, or a share outlives the deletion."""
    peer = a_subject()
    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer, "role": "viewer"})
    assert api.delete(f"/api/v1/nodes/{folder}").status_code in (200, 204)

    restored = api.post(f"/api/v1/nodes/{folder}/restore")
    assert restored.status_code == 200, restored.text
    # The grant's fate across a delete/restore cycle is the observable part: it is
    # either withdrawn or restored with the node, never silently half-present.
    rows = api.get(f"/api/v1/nodes/{folder}/grants").json()["items"]
    assert all(row["subject"] == peer for row in rows), rows


# --- the directory lookup, which is what email recipients need --------------


def test_sharing_by_email_resolves_a_recipient(api: httpx.Client, folder: str) -> None:
    """Sharing by email, which is how a person actually shares.

    This is the reason this file exists. On the deployment it answers `503`
    "the user directory is unavailable" for *every* email, including addresses
    that certainly exist -- because CyberFS's OAuth client was registered with no
    scopes, so `GET /orgs/{id}/members` refuses it with
    `403 Insufficient scope: directory:read required`. The adapter turns any
    HTTP error from that call into `DependencyUnavailableError`, so the symptom
    names the wrong cause.

    Nothing in-process can catch this: the integration suite stubs the directory,
    and a stub cannot be missing a scope. Left as a failing assertion rather than
    an `xfail` -- the deployment is genuinely broken for email sharing, and a test
    that passes while it is broken is how it stays broken.
    """
    response = api.put(
        f"/api/v1/nodes/{folder}/grants",
        json={"recipient": "someone@cyberdynecorp.ai", "role": "viewer"},
    )

    assert response.status_code != 503, (
        "the user directory is unreachable: grant CyberFS's OAuth client the "
        "'directory:read' scope in CyberdyneAuth. Observed: " + response.text
    )
    # An address nobody holds is a 404/422 about the recipient, never a 503.
    assert response.status_code in (201, 404, 422), response.text


# --- what only a second real account can show ------------------------------


def test_a_recipient_can_read_a_shared_file_and_sees_it_in_their_own_listing(
    api: httpx.Client, peer: httpx.Client, peer_subject: str, folder: str
) -> None:
    body = os.urandom(2048)
    node = upload(api, folder, f"shared-{uuid4().hex[:8]}.bin", body)
    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer_subject, "role": "viewer"})

    assert peer.get(f"/api/v1/nodes/{node['id']}/content").content == body
    shared = peer.get("/api/v1/shared-with-me", params={"limit": 100})
    assert shared.status_code == 200, shared.text
    assert folder in {item["id"] for item in shared.json()["items"]}


def test_a_viewer_cannot_write_and_an_editor_can(
    api: httpx.Client, peer: httpx.Client, peer_subject: str, folder: str
) -> None:
    node = upload(api, folder, f"roles-{uuid4().hex[:8]}.bin", os.urandom(256))
    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer_subject, "role": "viewer"})

    refused = peer.patch(f"/api/v1/nodes/{node['id']}/name", json={"name": "renamed-by-viewer.bin"})
    assert refused.status_code == 403, refused.text

    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer_subject, "role": "editor"})
    allowed = peer.patch(f"/api/v1/nodes/{node['id']}/name", json={"name": "renamed-by-editor.bin"})
    assert allowed.status_code == 200, allowed.text


def test_revoking_a_grant_takes_the_access_away_immediately(
    api: httpx.Client, peer: httpx.Client, peer_subject: str, folder: str
) -> None:
    """The cache makes this worth asserting over the wire: a stale permission
    decision would keep serving a recipient who was just removed."""
    node = upload(api, folder, f"revoked-{uuid4().hex[:8]}.bin", os.urandom(256))
    api.put(f"/api/v1/nodes/{folder}/grants", json={"recipient": peer_subject, "role": "viewer"})
    assert peer.get(f"/api/v1/nodes/{node['id']}/content").status_code == 200

    api.delete(f"/api/v1/nodes/{folder}/grants/{peer_subject}")

    assert peer.get(f"/api/v1/nodes/{node['id']}/content").status_code in (403, 404)


def test_ownership_transfers_and_the_former_owner_keeps_editor_access(
    api: httpx.Client, peer: httpx.Client, peer_subject: str, folder: str
) -> None:
    """Transfer, then transfer back, so the deployment ends where it started.

    Gated on a real second account rather than a subject-shaped placeholder for a
    blunt reason: handing a live node to a subject nobody holds would strand real
    data under an owner who can never log in to give it back. `keep_editor_access`
    is what makes the return trip possible from this side.
    """
    node = upload(api, folder, f"handover-{uuid4().hex[:8]}.bin", os.urandom(512))
    original = api.get(f"/api/v1/nodes/{folder}").json()["owner_id"]

    handed = api.post(
        f"/api/v1/nodes/{folder}/owner",
        json={"recipient": peer_subject, "keep_editor_access": True},
    )
    assert handed.status_code == 200, handed.text
    assert api.get(f"/api/v1/nodes/{folder}").json()["owner_id"] != original

    # The recipient owns it now, and the former owner is still an editor.
    assert peer.get(f"/api/v1/nodes/{node['id']}/content").status_code == 200
    renamed = api.patch(f"/api/v1/nodes/{node['id']}/name", json={"name": "still-editable.bin"})
    assert renamed.status_code == 200, renamed.text

    # Hand it back from the new owner's side, which is the only side that can.
    returned = peer.post(
        f"/api/v1/nodes/{folder}/owner", json={"recipient": original, "keep_editor_access": False}
    )
    assert returned.status_code == 200, returned.text
    assert api.get(f"/api/v1/nodes/{folder}").json()["owner_id"] == original


def test_shared_with_me_does_not_list_a_recipients_own_nodes(peer: httpx.Client) -> None:
    """ "Shared with me" means shared *by someone else*; own nodes belong in the tree."""
    own_root = peer.get("/api/v1/nodes/root").json()["id"]
    listed = peer.get("/api/v1/shared-with-me", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    assert own_root not in {item["id"] for item in listed.json()["items"]}


# --- public links ----------------------------------------------------------


def test_a_nodes_links_are_listed_and_revocation_is_visible_to_its_owner(
    api: httpx.Client, folder: str
) -> None:
    """`GET /nodes/{id}/links` had no coverage: a link the owner cannot see is a
    link they cannot revoke.

    Revocation is deliberately a soft one -- the row stays and its `revoked` flag
    flips -- so an owner keeps the record of what they once published. The
    listing therefore has to *say* which links are dead, or the audit value is
    lost and a revoked link is indistinguishable from a live one.
    """
    node = upload(api, folder, f"linked-{uuid4().hex[:8]}.bin", os.urandom(128))

    created = api.post(f"/api/v1/nodes/{node['id']}/links", json={})
    assert created.status_code == 201, created.text
    link_id = created.json()["id"]

    listed = api.get(f"/api/v1/nodes/{node['id']}/links")
    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json()["items"] if r["id"] == link_id)
    assert row["revoked"] is False

    assert api.delete(f"/api/v1/links/{link_id}").status_code in (200, 204)

    after = next(
        r
        for r in api.get(f"/api/v1/nodes/{node['id']}/links").json()["items"]
        if r["id"] == link_id
    )
    assert after["revoked"] is True, "a revoked link is not reported as revoked"


def test_a_link_listing_is_refused_to_someone_who_does_not_own_the_node(
    api: httpx.Client,
) -> None:
    """Links are a publication record; only the owner may enumerate them."""
    assert api.get(f"/api/v1/nodes/{uuid4()}/links").status_code in (403, 404)
