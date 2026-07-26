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

WARNING: this writes real data. Everything is created inside one scratch folder
that is removed afterwards, but the writes reach the deployment's Postgres and
object store, and the operations appear in its audit log and activity feed.
"""

from __future__ import annotations

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

    # DELETE is a move to trash, not a purge -- there is no purge endpoint. The
    # deployment's trash retention reclaims it later.
    api.delete(f"/api/v1/nodes/{folder_id}")


@pytest.fixture
def folder(api: httpx.Client, scratch: str) -> str:
    """A fresh subfolder per test, so tests cannot collide on names."""
    response = api.post(
        f"/api/v1/nodes/{scratch}/folders", json={"name": f"case-{uuid4().hex[:8]}"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


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
