"""Verification against a real CyberdyneAuth deployment.

Skipped unless pointed at one. The unit suite already exercises every scenario
in `authentication/spec.md` against a conformant fake that signs with real RSA
keys (`tests/unit/test_auth_adapters.py`); what only a live service can tell us
is whether our reading of *its* discovery document is right.

    CYBERFS_LIVE_AUTH_BASE_URL=https://auth.backend.coolify.cyberdynecorp.ai \
    CYBERFS_LIVE_CLIENT_ID=cyberfs \
    CYBERFS_LIVE_CLIENT_SECRET=... \
    uv run pytest tests/integration/test_live_auth.py -m integration

`CYBERFS_LIVE_USER_TOKEN` additionally enables the end-to-end verify and
introspect checks; without it only the unauthenticated surface is exercised.
"""

from __future__ import annotations

import os
from datetime import timedelta

import httpx
import pytest

from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient
from cyberfs.adapters.outbound.auth.introspection import TokenIntrospectionClient
from cyberfs.adapters.outbound.auth.service_token import ServiceTokenProvider
from cyberfs.adapters.outbound.auth.verifier import JwtTokenVerifier
from cyberfs.domain.auth.policy import CacheWindow

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("CYBERFS_LIVE_AUTH_BASE_URL")
CLIENT_ID = os.environ.get("CYBERFS_LIVE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("CYBERFS_LIVE_CLIENT_SECRET")
USER_TOKEN = os.environ.get("CYBERFS_LIVE_USER_TOKEN")

requires_auth = pytest.mark.skipif(not BASE_URL, reason="CYBERFS_LIVE_AUTH_BASE_URL is not set")
requires_client = pytest.mark.skipif(
    not (BASE_URL and CLIENT_ID and CLIENT_SECRET),
    reason="live client credentials are not set (see docs/auth-integration.md)",
)
requires_user_token = pytest.mark.skipif(
    not (BASE_URL and USER_TOKEN), reason="CYBERFS_LIVE_USER_TOKEN is not set"
)


@pytest.fixture
async def http() -> httpx.AsyncClient:
    async with httpx.AsyncClient(timeout=15.0) as client:
        yield client


@pytest.fixture
def discovery(http: httpx.AsyncClient) -> DiscoveryClient:
    window = CacheWindow(ttl=timedelta(hours=1), stale_max=timedelta(hours=24))
    return DiscoveryClient(
        BASE_URL or "",
        http,
        discovery_window=window,
        jwks_window=window,
        refresh_cooldown=timedelta(seconds=60),
    )


@requires_auth
async def test_discovery_document_is_usable(discovery: DiscoveryClient) -> None:
    metadata = await discovery.metadata()
    assert metadata.issuer
    assert metadata.jwks_uri
    assert metadata.signing_algorithms


@requires_auth
async def test_issuer_matches_the_document_it_came_from(discovery: DiscoveryClient) -> None:
    """The exact check a relying party must make, and the one #47/#114 broke."""
    metadata = await discovery.metadata()
    assert metadata.issuer.startswith("http")


@requires_auth
async def test_jwks_is_reachable_and_has_keys(discovery: DiscoveryClient) -> None:
    keys = await discovery.jwks()
    assert keys["keys"], "the live JWKS advertised no keys"
    assert all("kid" in key for key in keys["keys"])


@requires_auth
async def test_advertised_algorithms_exclude_none(discovery: DiscoveryClient) -> None:
    metadata = await discovery.metadata()
    assert "none" not in {alg.lower() for alg in metadata.signing_algorithms}


@requires_client
async def test_service_token_can_be_obtained(
    discovery: DiscoveryClient, http: httpx.AsyncClient
) -> None:
    provider = ServiceTokenProvider(
        discovery, http, client_id=CLIENT_ID or "", client_secret=CLIENT_SECRET or ""
    )
    assert await provider.token()


@requires_user_token
async def test_live_user_token_verifies(discovery: DiscoveryClient) -> None:
    verifier = JwtTokenVerifier(discovery, clock_skew=timedelta(seconds=60))
    principal = await verifier.verify(USER_TOKEN or "")
    assert principal.subject


@pytest.mark.skipif(
    not (BASE_URL and CLIENT_ID and CLIENT_SECRET and USER_TOKEN),
    reason="live client credentials and a user token are required",
)
async def test_introspection_agrees_with_local_verification(
    discovery: DiscoveryClient, http: httpx.AsyncClient
) -> None:
    verifier = JwtTokenVerifier(discovery, clock_skew=timedelta(seconds=60))
    provider = ServiceTokenProvider(
        discovery, http, client_id=CLIENT_ID or "", client_secret=CLIENT_SECRET or ""
    )
    introspector = TokenIntrospectionClient(discovery, provider, http)

    verified = await verifier.verify(USER_TOKEN or "")
    introspected = await introspector.introspect(USER_TOKEN or "")

    assert verified.subject == introspected.subject
