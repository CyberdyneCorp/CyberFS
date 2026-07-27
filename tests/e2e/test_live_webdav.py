"""The WebDAV surface, against a deployed CyberFS over its real ingress.

This tier is the only one that can say anything about WebDAV at all. The unit
tests build XML from `Node` objects, and the integration tests mount the ASGI app
directly -- neither of them crosses a reverse proxy, and a proxy is exactly what
WebDAV trips over: `PROPFIND`, `MKCOL`, `COPY` and `MOVE` are not methods every
front end forwards, `Destination` is a header some rewrite, and a `207` is a
status some collapse. The feature was enabled by default on the strength of tests
that could not see any of that.

Authenticated with an S3 access key over HTTP Basic, which is what a real client
sends. That also makes this the only place the two credentials-bearing surfaces
are shown to accept the same key.
"""

from __future__ import annotations

import os
from uuid import uuid4
from xml.etree import ElementTree

import httpx
import pytest

from .conftest import requires_deployment

pytestmark = [pytest.mark.e2e, requires_deployment]

DAV = "{DAV:}"


def propfind(dav: httpx.Client, path: str, depth: str = "1") -> httpx.Response:
    return dav.request("PROPFIND", path, headers={"Depth": depth})


def hrefs(body: str) -> set[str]:
    root = ElementTree.fromstring(body)  # noqa: S314 - our own server's output
    return {
        (element.text or "")
        for response in root.findall(f"{DAV}response")
        for element in response.findall(f"{DAV}href")
    }


# --- the surface exists, and says what it is --------------------------------


def test_options_advertises_class_1_and_its_methods_before_authenticating(
    anonymous: httpx.Client,
) -> None:
    """A client discovers the surface before it has sent a credential.

    Answered pre-auth deliberately: a `401` here would leave a client unable to
    tell "no WebDAV" from "wrong password". It discloses nothing but the shape of
    the protocol.
    """
    response = anonymous.request("OPTIONS", "/webdav")

    assert response.status_code == 200, response.text
    assert response.headers["DAV"] == "1"
    advertised = {method.strip() for method in response.headers["Allow"].split(",")}
    assert {"PROPFIND", "GET", "PUT", "DELETE", "MKCOL", "COPY", "MOVE"} <= advertised
    assert "LOCK" not in advertised, "class 1 must not advertise locking"


def test_an_unauthenticated_request_is_challenged_and_discloses_nothing(
    anonymous: httpx.Client,
) -> None:
    response = anonymous.request("PROPFIND", "/webdav", headers={"Depth": "0"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic ")
    assert "multistatus" not in response.text


def test_a_wrong_secret_is_refused(s3_key: tuple[str, str], anonymous: httpx.Client) -> None:
    key_id, _ = s3_key
    response = anonymous.request(
        "PROPFIND", "/webdav", headers={"Depth": "0"}, auth=httpx.BasicAuth(key_id, "not-it")
    )
    assert response.status_code == 401


@pytest.mark.parametrize("method", ["LOCK", "UNLOCK", "PROPPATCH"])
def test_the_methods_class_1_does_not_implement_are_refused(dav: httpx.Client, method: str) -> None:
    """405 rather than 404: the surface is there, it just will not do this.

    A client that reads `DAV: 1` and tries anyway should learn that from the
    status, not from a parse error.
    """
    assert dav.request(method, "/webdav").status_code == 405


# --- reading the tree ------------------------------------------------------


def test_propfind_on_the_root_is_a_multistatus_naming_the_root_itself(
    dav: httpx.Client,
) -> None:
    response = propfind(dav, "/webdav", depth="0")

    assert response.status_code == 207, response.text
    assert response.headers["content-type"].startswith("application/xml")
    assert hrefs(response.text) == {"/webdav/"}


def test_depth_one_lists_children_and_marks_collections_with_a_trailing_slash(
    dav: httpx.Client, scratch_name: str
) -> None:
    response = propfind(dav, "/webdav", depth="1")

    assert response.status_code == 207, response.text
    assert f"/webdav/{scratch_name}/" in hrefs(response.text), (
        "the scratch folder must appear as a collection, with the slash clients rely on"
    )


def test_depth_infinity_is_refused_rather_than_walking_the_whole_tree(
    dav: httpx.Client,
) -> None:
    """An unbounded recursive walk in one request is how a DAV server is made to
    exhaust itself. RFC 4918 permits refusing it, and most servers do."""
    assert propfind(dav, "/webdav", depth="infinity").status_code == 403


def test_an_absent_path_is_not_found(dav: httpx.Client) -> None:
    assert propfind(dav, f"/webdav/no-such-{uuid4().hex[:8]}", depth="0").status_code == 404


# --- writing over WebDAV ---------------------------------------------------


def test_a_file_put_over_webdav_reads_back_byte_for_byte(dav: httpx.Client, dav_dir: str) -> None:
    body = os.urandom(4096)

    written = dav.request("PUT", f"{dav_dir}/payload.bin", content=body)
    assert written.status_code in (201, 204), written.text

    fetched = dav.request("GET", f"{dav_dir}/payload.bin")
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == body, "WebDAV round trip altered the bytes"


def test_head_reports_the_size_without_the_body(dav: httpx.Client, dav_dir: str) -> None:
    body = os.urandom(1024)
    dav.request("PUT", f"{dav_dir}/sized.bin", content=body)

    response = dav.request("HEAD", f"{dav_dir}/sized.bin")

    assert response.status_code == 200
    assert response.headers["Content-Length"] == str(len(body))
    assert response.content == b""


def test_mkcol_creates_a_collection_that_propfind_then_reports(
    dav: httpx.Client, dav_dir: str
) -> None:
    created = dav.request("MKCOL", f"{dav_dir}/nested")
    assert created.status_code == 201, created.text

    listed = propfind(dav, f"{dav_dir}/nested", depth="0")
    assert listed.status_code == 207
    assert "<D:collection/>" in listed.text


def test_mkcol_on_an_existing_collection_is_405_not_412(dav: httpx.Client, dav_dir: str) -> None:
    """RFC 4918 9.3.1 names 405 for a mapped URL, and clients act on it.

    A sync client calls MKCOL on directories that may already exist and reads 405
    as "already there, carry on". The 412 this surface returned for every other
    taken-name refusal reads instead as a precondition the client never set.
    """
    assert dav.request("MKCOL", f"{dav_dir}/twice").status_code == 201
    assert dav.request("MKCOL", f"{dav_dir}/twice").status_code == 405


def test_copy_leaves_the_source_in_place(dav: httpx.Client, dav_dir: str) -> None:
    body = os.urandom(512)
    dav.request("PUT", f"{dav_dir}/original.bin", content=body)

    copied = dav.request(
        "COPY", f"{dav_dir}/original.bin", headers={"Destination": f"{dav_dir}/duplicate.bin"}
    )

    assert copied.status_code in (201, 204), copied.text
    assert dav.request("GET", f"{dav_dir}/duplicate.bin").content == body
    assert dav.request("GET", f"{dav_dir}/original.bin").content == body, "COPY moved it"


def test_move_takes_the_source_away(dav: httpx.Client, dav_dir: str) -> None:
    body = os.urandom(512)
    dav.request("PUT", f"{dav_dir}/before.bin", content=body)

    moved = dav.request(
        "MOVE", f"{dav_dir}/before.bin", headers={"Destination": f"{dav_dir}/after.bin"}
    )

    assert moved.status_code in (201, 204), moved.text
    assert dav.request("GET", f"{dav_dir}/after.bin").content == body
    assert dav.request("GET", f"{dav_dir}/before.bin").status_code == 404


def test_delete_removes_a_file(dav: httpx.Client, dav_dir: str) -> None:
    dav.request("PUT", f"{dav_dir}/doomed.bin", content=b"gone shortly")

    assert dav.request("DELETE", f"{dav_dir}/doomed.bin").status_code in (200, 204)
    assert dav.request("GET", f"{dav_dir}/doomed.bin").status_code == 404


def test_a_name_needing_percent_encoding_survives_the_round_trip(
    dav: httpx.Client, dav_dir: str
) -> None:
    """A space and an ampersand in one name, through a real proxy.

    The encoding is per segment, so a name like this must address the file it
    names rather than truncating the path -- and the proxy has to pass it
    through unmangled, which only this tier can show.
    """
    body = os.urandom(256)
    name = "a b & c.bin"

    written = dav.request("PUT", f"{dav_dir}/a%20b%20%26%20c.bin", content=body)
    assert written.status_code in (201, 204), written.text

    listing = propfind(dav, dav_dir, depth="1")
    assert "a b &amp; c.bin" in listing.text, listing.text
    assert dav.request("GET", f"{dav_dir}/a%20b%20%26%20c.bin").content == body
    assert name  # the decoded form is what the listing above reports


# --- the two surfaces describe one filesystem ------------------------------


def test_a_file_written_over_webdav_is_visible_over_rest_with_the_same_etag(
    dav: httpx.Client, api: httpx.Client, dav_dir: str, scratch: str
) -> None:
    """The invariant that makes a second surface safe rather than a second store.

    A client that caches on one surface and revalidates on the other must not be
    told the same state has two tags.
    """
    body = os.urandom(2048)
    dav.request("PUT", f"{dav_dir}/shared-view.bin", content=body)

    dav_etag = dav.request("HEAD", f"{dav_dir}/shared-view.bin").headers.get("ETag")
    assert dav_etag, "WebDAV served no ETag"

    # Find the same node through the REST tree: the DAV path's last two segments
    # are the collection MKCOL made and the file PUT wrote.
    collection = dav_dir.rsplit("/", 1)[-1]
    children = api.get(f"/api/v1/nodes/{scratch}/children", params={"limit": 100})
    assert children.status_code == 200, children.text
    folder = next(c for c in children.json()["items"] if c["name"] == collection)

    inside = api.get(f"/api/v1/nodes/{folder['id']}/children", params={"limit": 100})
    node = next(c for c in inside.json()["items"] if c["name"] == "shared-view.bin")

    assert node["size_bytes"] == len(body)
    detail = api.get(f"/api/v1/nodes/{node['id']}")
    assert detail.headers.get("ETag") == dav_etag, "the two surfaces disagree about the ETag"
    assert api.get(f"/api/v1/nodes/{node['id']}/content").content == body


def test_a_file_created_over_rest_is_readable_over_webdav(
    dav: httpx.Client, api: httpx.Client, dav_dir: str, scratch_name: str
) -> None:
    from .conftest import upload

    collection = dav_dir.rsplit("/", 1)[-1]
    listing = propfind(dav, dav_dir, depth="0")
    assert listing.status_code == 207, "the collection must exist before REST writes into it"

    # Resolve the collection's node id by name, then write through REST.
    root = api.get("/api/v1/nodes/root").json()["id"]
    scratch_node = next(
        c
        for c in api.get(f"/api/v1/nodes/{root}/children", params={"limit": 200}).json()["items"]
        if c["name"] == scratch_name
    )
    folder = next(
        c
        for c in api.get(
            f"/api/v1/nodes/{scratch_node['id']}/children", params={"limit": 100}
        ).json()["items"]
        if c["name"] == collection
    )

    body = os.urandom(1024)
    upload(api, str(folder["id"]), "from-rest.bin", body)

    assert dav.request("GET", f"{dav_dir}/from-rest.bin").content == body


def test_an_encrypted_file_is_served_decrypted_over_webdav(
    dav: httpx.Client, api: httpx.Client, dav_dir: str, scratch_name: str
) -> None:
    """WebDAV has no way to say "encrypted", so it must serve plaintext.

    Otherwise a client mounting the share would silently download ciphertext it
    cannot read, which is worse than refusing.
    """
    from .conftest import upload

    collection = dav_dir.rsplit("/", 1)[-1]
    root = api.get("/api/v1/nodes/root").json()["id"]
    scratch_node = next(
        c
        for c in api.get(f"/api/v1/nodes/{root}/children", params={"limit": 200}).json()["items"]
        if c["name"] == scratch_name
    )
    folder = next(
        c
        for c in api.get(
            f"/api/v1/nodes/{scratch_node['id']}/children", params={"limit": 100}
        ).json()["items"]
        if c["name"] == collection
    )

    body = os.urandom(3000)
    created = upload(api, str(folder["id"]), "secret.bin", body, encrypted=True)
    assert created["encrypted"] is True, created

    assert dav.request("GET", f"{dav_dir}/secret.bin").content == body


# --- the credential is the S3 key, with the same lifecycle -----------------


def test_a_revoked_key_stops_working_on_the_webdav_surface(
    api: httpx.Client, anonymous: httpx.Client
) -> None:
    """Revocation has to reach every surface the key opens, not just S3."""
    created = api.post("/api/v1/me/s3-keys", json={"label": f"revoke-probe-{uuid4().hex[:8]}"})
    assert created.status_code in (200, 201), created.text
    body = created.json()
    auth = httpx.BasicAuth(str(body["access_key_id"]), str(body["secret_access_key"]))

    before = anonymous.request("PROPFIND", "/webdav", headers={"Depth": "0"}, auth=auth)
    assert before.status_code == 207, before.text

    revoked = api.delete(f"/api/v1/me/s3-keys/{body['access_key_id']}")
    assert revoked.status_code in (200, 204), revoked.text

    after = anonymous.request("PROPFIND", "/webdav", headers={"Depth": "0"}, auth=auth)
    assert after.status_code == 401, "a revoked key still opened the WebDAV surface"
