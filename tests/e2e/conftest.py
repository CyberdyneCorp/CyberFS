"""Fixtures for the live end-to-end suite.

These tests speak HTTP to a *deployed* CyberFS over its real ingress. That is
the point: the in-process integration suite mounts the ASGI app directly, so it
cannot see a missing environment variable, a stale CORS allowlist, a proxy that
strips a header, or a migration that never ran. Everything here goes over the
wire with a real CyberdyneAuth token.

Skipped entirely unless pointed at a deployment, so `just test` is unaffected.

    CYBERFS_LIVE_API_BASE_URL=https://cyberfs.backend.coolify.cyberdynecorp.ai \
    CYBERFS_LIVE_AUTH_BASE_URL=https://auth.backend.coolify.cyberdynecorp.ai \
    CYBERFS_LIVE_EMAIL=someone@example.com \
    CYBERFS_LIVE_PASSWORD=... \
    uv run pytest tests/e2e -m e2e

Supply `CYBERFS_LIVE_USER_TOKEN` instead of the email/password pair when you
already hold a token, or when the account has a second factor -- the password
grant here cannot answer an MFA challenge.

Optional, each unlocking tests that skip without it:

    CYBERFS_LIVE_SECOND_EMAIL / CYBERFS_LIVE_SECOND_PASSWORD
        (or CYBERFS_LIVE_SECOND_USER_TOKEN) -- a second account, for the sharing
        assertions that need a real recipient: reading shared content, "shared
        with me" from the other side, role enforcement, and ownership transfer.
    CYBERFS_LIVE_RUN_BACKUP=1
        -- start a real backup, which runs `pg_dump` on the deployment and
        uploads the result. Reading the backup register needs no opt-in.

Coverage is per API operation: `tests/e2e/` reaches every route the deployment
publishes, plus the WebDAV surface, which is absent from the OpenAPI document by
design. The S3 tier skips unless `S3_API_ENABLED` is set on the deployment, and
says so rather than passing vacuously.

WARNING: this writes real data. Everything is created inside one scratch folder
that is removed afterwards, but the writes reach the deployment's Postgres and
object store, and the operations appear in its audit log and activity feed.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

API_BASE_URL = os.environ.get("CYBERFS_LIVE_API_BASE_URL")
AUTH_BASE_URL = os.environ.get("CYBERFS_LIVE_AUTH_BASE_URL")
USER_TOKEN = os.environ.get("CYBERFS_LIVE_USER_TOKEN")
EMAIL = os.environ.get("CYBERFS_LIVE_EMAIL")
PASSWORD = os.environ.get("CYBERFS_LIVE_PASSWORD")

#: A second real account, for the assertions that need two sides: that a
#: recipient can actually read what was shared, that it shows up in *their*
#: "shared with me", and that ownership transfer lands somewhere real. Optional,
#: because most of the sharing surface is provable with a subject-shaped
#: recipient that never has to log in.
SECOND_TOKEN = os.environ.get("CYBERFS_LIVE_SECOND_USER_TOKEN")
SECOND_EMAIL = os.environ.get("CYBERFS_LIVE_SECOND_EMAIL")
SECOND_PASSWORD = os.environ.get("CYBERFS_LIVE_SECOND_PASSWORD")

#: Opt-in, because it starts a real backup on the deployment: a pg_dump plus an
#: upload to the backup bucket. Reading the backup list needs no opt-in.
RUN_BACKUP = os.environ.get("CYBERFS_LIVE_RUN_BACKUP") == "1"

TIMEOUT = httpx.Timeout(30.0)

#: Every scratch folder starts with this, so anything left behind by a crashed
#: run is identifiable and safe to remove by hand.
SCRATCH_PREFIX = "cyberfs-e2e-"


def _have_credentials() -> bool:
    return bool(USER_TOKEN or (AUTH_BASE_URL and EMAIL and PASSWORD))


requires_deployment = pytest.mark.skipif(
    not (API_BASE_URL and _have_credentials()),
    reason="set CYBERFS_LIVE_API_BASE_URL plus CYBERFS_LIVE_USER_TOKEN, "
    "or CYBERFS_LIVE_AUTH_BASE_URL with CYBERFS_LIVE_EMAIL and CYBERFS_LIVE_PASSWORD",
)


def mint_user_token() -> str:
    """Exchange the configured password for a user access token.

    A client-credentials token is deliberately not accepted: its subject is
    `client:<id>` rather than a user, so it cannot own a filesystem tree.
    """
    if USER_TOKEN:
        return USER_TOKEN

    assert AUTH_BASE_URL and EMAIL and PASSWORD  # guarded by `requires_deployment`
    response = httpx.post(
        f"{AUTH_BASE_URL.rstrip('/')}/api/v1/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("mfa_required"):
        pytest.skip("account requires a second factor; supply CYBERFS_LIVE_USER_TOKEN instead")
    token = body.get("access_token")
    if not token:
        pytest.fail("CyberdyneAuth returned no access_token")
    return str(token)


@pytest.fixture(scope="session")
def token() -> str:
    return mint_user_token()


@pytest.fixture(scope="session")
def api(token: str) -> Iterator[httpx.Client]:
    """An authenticated client bound to the deployment."""
    assert API_BASE_URL
    with httpx.Client(
        base_url=API_BASE_URL.rstrip("/"),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=TIMEOUT,
        follow_redirects=True,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def anonymous() -> Iterator[httpx.Client]:
    """An unauthenticated client, for the public-link and refusal checks."""
    assert API_BASE_URL
    with httpx.Client(base_url=API_BASE_URL.rstrip("/"), timeout=TIMEOUT) as client:
        yield client


@pytest.fixture(scope="session")
def root_id(api: httpx.Client) -> str:
    response = api.get("/api/v1/nodes/root")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


@pytest.fixture(scope="session")
def scratch(api: httpx.Client, root_id: str) -> Iterator[str]:
    """One throwaway folder for the whole run, trashed on the way out.

    Session-scoped so a failure mid-suite still leaves exactly one identifiable
    folder rather than a scatter of them.
    """
    name = f"{SCRATCH_PREFIX}{uuid4().hex[:12]}"
    created = api.post(f"/api/v1/nodes/{root_id}/folders", json={"name": name})
    assert created.status_code == 201, created.text
    folder_id = str(created.json()["id"])

    yield folder_id

    # Trash then purge, so the run leaves nothing occupying the deployment's
    # quota. Purge requires the node to be in the trash first, by design.
    api.delete(f"/api/v1/nodes/{folder_id}")
    api.post(f"/api/v1/nodes/{folder_id}/purge")


@pytest.fixture
def folder(api: httpx.Client, scratch: str) -> str:
    """A fresh subfolder per test, so tests cannot collide on names."""
    response = api.post(
        f"/api/v1/nodes/{scratch}/folders", json={"name": f"case-{uuid4().hex[:8]}"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def subject_of(access_token: str) -> str:
    """The `sub` claim, which is what a grant names and the admin list reports.

    Read straight off the token rather than from an endpoint, because there is
    none: `/me/activity` returns the caller's *own* feed, so it never names the
    actor, and no other route hands a caller their subject.

    Unverified on purpose -- the signature is CyberFS's business, and this only
    needs to know which account the suite is running as.
    """
    payload = access_token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(padded))["sub"])


@pytest.fixture(scope="session")
def subject(token: str) -> str:
    return subject_of(token)


@pytest.fixture(scope="session")
def is_admin(api: httpx.Client) -> bool:
    """Whether the configured account may reach `/api/v1/admin/*`.

    Probed rather than assumed: the admin tests are worth running when the
    account has the rights and worth skipping clearly when it does not, and
    nothing in the token says so in a form this suite should parse itself.
    """
    return api.get("/api/v1/admin/overview").status_code == 200


@pytest.fixture(scope="session")
def s3_key(api: httpx.Client) -> Iterator[tuple[str, str]]:
    """A live S3 access key, revoked on the way out.

    The credential the WebDAV and S3 surfaces take. Minted per session because
    the secret is shown exactly once, at creation -- there is no endpoint that
    will hand it back later, by design.
    """
    created = api.post("/api/v1/me/s3-keys", json={"label": f"{SCRATCH_PREFIX}{uuid4().hex[:8]}"})
    assert created.status_code in (200, 201), created.text
    body = created.json()
    key_id, secret = str(body["access_key_id"]), str(body["secret_access_key"])

    yield key_id, secret

    api.delete(f"/api/v1/me/s3-keys/{key_id}")


@pytest.fixture(scope="session")
def dav(s3_key: tuple[str, str]) -> Iterator[httpx.Client]:
    """A WebDAV client authenticated with Basic and that access key."""
    assert API_BASE_URL
    key_id, secret = s3_key
    with httpx.Client(
        base_url=API_BASE_URL.rstrip("/"),
        auth=httpx.BasicAuth(key_id, secret),
        timeout=TIMEOUT,
        # Deliberately off: a redirect on a WebDAV method is a routing bug, and
        # following it would hide the status the client actually received.
        follow_redirects=False,
    ) as client:
        yield client


@pytest.fixture
def dav_dir(dav: httpx.Client, scratch_name: str) -> Iterator[str]:
    """A collection created over WebDAV itself, removed the same way."""
    path = f"/webdav/{scratch_name}/dav-{uuid4().hex[:8]}"
    created = dav.request("MKCOL", path)
    assert created.status_code in (201, 405), created.text
    yield path
    dav.request("DELETE", path)


@pytest.fixture(scope="session")
def scratch_name(api: httpx.Client, scratch: str) -> str:
    """The scratch folder's name, which is how WebDAV addresses it.

    WebDAV paths are names, not identifiers, so the two surfaces need this to
    agree in order to be pointed at the same subtree.
    """
    response = api.get(f"/api/v1/nodes/{scratch}")
    assert response.status_code == 200, response.text
    return str(response.json()["name"])


def second_client() -> httpx.Client | None:
    """A client for the optional second account, or None if unconfigured."""
    if not API_BASE_URL:
        return None
    token = SECOND_TOKEN
    if token is None and AUTH_BASE_URL and SECOND_EMAIL and SECOND_PASSWORD:
        response = httpx.post(
            f"{AUTH_BASE_URL.rstrip('/')}/api/v1/auth/login",
            json={"email": SECOND_EMAIL, "password": SECOND_PASSWORD},
            timeout=TIMEOUT,
        )
        if response.status_code != 200 or response.json().get("mfa_required"):
            return None
        token = response.json().get("access_token")
    if not token:
        return None
    return httpx.Client(
        base_url=API_BASE_URL.rstrip("/"),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


@pytest.fixture(scope="session")
def peer() -> Iterator[httpx.Client]:
    """The second account, skipping the test when none is configured."""
    client = second_client()
    if client is None:
        pytest.skip(
            "set CYBERFS_LIVE_SECOND_USER_TOKEN, or CYBERFS_LIVE_SECOND_EMAIL with "
            "CYBERFS_LIVE_SECOND_PASSWORD, to exercise the two-sided sharing assertions"
        )
    with client:
        yield client


@pytest.fixture(scope="session")
def peer_subject(peer: httpx.Client) -> str:
    """The second account's subject, taken from its own token."""
    token = peer.headers["Authorization"].removeprefix("Bearer ").strip()
    return subject_of(token)


def upload(
    api: httpx.Client,
    parent_id: str,
    name: str,
    body: bytes,
    *,
    encrypted: bool | None = None,
) -> dict:
    """PUT a new file and return its summary."""
    params = {} if encrypted is None else {"encrypted": str(encrypted).lower()}
    response = api.put(
        f"/api/v1/nodes/{parent_id}/files/{name}",
        content=body,
        params=params,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())
