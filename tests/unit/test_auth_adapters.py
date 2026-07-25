"""Discovery, JWKS, verification, service tokens, and introspection.

Exercised against a fake CyberdyneAuth that signs with real RSA keys.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx

from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient, parse_discovery
from cyberfs.adapters.outbound.auth.introspection import TokenIntrospectionClient
from cyberfs.adapters.outbound.auth.service_token import ServiceTokenProvider
from cyberfs.adapters.outbound.auth.verifier import JwtTokenVerifier
from cyberfs.domain.auth.policy import CacheWindow
from cyberfs.domain.errors import (
    DependencyUnavailableError,
    InvalidTokenError,
    TokenExpiredError,
)

from .fake_auth import (
    DISCOVERY_URL,
    INTROSPECTION_URL,
    ISSUER,
    JWKS_URL,
    TOKEN_URL,
    SigningKey,
    discovery_document,
    jwks_document,
    user_claims,
)

SKEW = timedelta(seconds=60)


@pytest.fixture
def key() -> SigningKey:
    return SigningKey("key-1")


@pytest.fixture
def http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=2.0)


def make_discovery(
    http: httpx.AsyncClient,
    *,
    ttl: timedelta = timedelta(hours=1),
    stale_max: timedelta = timedelta(hours=24),
    cooldown: timedelta = timedelta(seconds=60),
) -> DiscoveryClient:
    window = CacheWindow(ttl=ttl, stale_max=stale_max)
    return DiscoveryClient(
        ISSUER, http, discovery_window=window, jwks_window=window, refresh_cooldown=cooldown
    )


def mock_auth(router: respx.Router, key: SigningKey, **document: object) -> None:
    router.get(DISCOVERY_URL).respond(json=discovery_document(**document))  # type: ignore[arg-type]
    router.get(JWKS_URL).respond(json=jwks_document(key))


# --- discovery parsing -----------------------------------------------------


def test_discovery_is_parsed() -> None:
    metadata = parse_discovery(discovery_document())
    assert metadata.issuer == ISSUER
    assert metadata.jwks_uri == JWKS_URL
    assert metadata.signing_algorithms == ("RS256",)
    assert metadata.introspection_endpoint == INTROSPECTION_URL


def test_discovery_without_optional_endpoints() -> None:
    document = discovery_document()
    del document["introspection_endpoint"]
    del document["token_endpoint"]
    metadata = parse_discovery(document)
    assert metadata.introspection_endpoint is None
    assert metadata.token_endpoint is None


# --- discovery fetching and caching ----------------------------------------


@respx.mock
async def test_discovery_is_fetched_and_cached(http: httpx.AsyncClient, key: SigningKey) -> None:
    route = respx.get(DISCOVERY_URL).respond(json=discovery_document())
    client = make_discovery(http)

    first = await client.metadata()
    second = await client.metadata()

    assert first.issuer == second.issuer == ISSUER
    assert route.call_count == 1, "the document must be reused within its TTL"


@respx.mock
async def test_discovery_is_refetched_after_ttl(http: httpx.AsyncClient) -> None:
    route = respx.get(DISCOVERY_URL).respond(json=discovery_document())
    client = make_discovery(http, ttl=timedelta(seconds=30))
    now = datetime.now(tz=UTC)

    await client.metadata(now)
    await client.metadata(now + timedelta(seconds=31))

    assert route.call_count == 2


@respx.mock
async def test_concurrent_cold_misses_make_one_request(http: httpx.AsyncClient) -> None:
    import asyncio

    route = respx.get(DISCOVERY_URL).respond(json=discovery_document())
    client = make_discovery(http)

    await asyncio.gather(*(client.metadata() for _ in range(8)))

    assert route.call_count == 1


@respx.mock
async def test_cold_cache_and_auth_down_is_a_dependency_error(http: httpx.AsyncClient) -> None:
    """`authentication/spec.md`: 503 when nothing usable is cached."""
    respx.get(DISCOVERY_URL).mock(side_effect=httpx.ConnectError("refused"))
    client = make_discovery(http)

    with pytest.raises(DependencyUnavailableError):
        await client.metadata()


@respx.mock
async def test_warm_cache_survives_an_auth_outage(http: httpx.AsyncClient) -> None:
    route = respx.get(DISCOVERY_URL).respond(json=discovery_document())
    client = make_discovery(http, ttl=timedelta(seconds=30), stale_max=timedelta(hours=24))
    now = datetime.now(tz=UTC)
    await client.metadata(now)

    route.mock(side_effect=httpx.ConnectError("refused"))
    metadata = await client.metadata(now + timedelta(minutes=5))

    assert metadata.issuer == ISSUER


@respx.mock
async def test_cache_past_the_stale_window_is_not_used(http: httpx.AsyncClient) -> None:
    route = respx.get(DISCOVERY_URL).respond(json=discovery_document())
    client = make_discovery(http, ttl=timedelta(seconds=30), stale_max=timedelta(hours=1))
    now = datetime.now(tz=UTC)
    await client.metadata(now)

    route.mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(DependencyUnavailableError):
        await client.metadata(now + timedelta(hours=2))


@respx.mock
async def test_discovery_http_error_is_a_dependency_error(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(status_code=500)
    with pytest.raises(DependencyUnavailableError):
        await make_discovery(http).metadata()


# --- JWKS ------------------------------------------------------------------


@respx.mock
async def test_jwks_is_fetched_from_the_discovered_uri(
    http: httpx.AsyncClient, key: SigningKey
) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    route = respx.get(JWKS_URL).respond(json=jwks_document(key))

    keys = await make_discovery(http).jwks()

    assert route.called
    assert keys["keys"][0]["kid"] == "key-1"


@respx.mock
async def test_unknown_kid_triggers_one_refresh(http: httpx.AsyncClient, key: SigningKey) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    route = respx.get(JWKS_URL).respond(json=jwks_document(key))
    client = make_discovery(http, cooldown=timedelta(seconds=60))
    now = datetime.now(tz=UTC)

    await client.jwks(now)
    await client.refresh_jwks_for_unknown_kid(now + timedelta(seconds=1))

    assert route.call_count == 2


@respx.mock
async def test_refresh_is_suppressed_within_the_cooldown(
    http: httpx.AsyncClient, key: SigningKey
) -> None:
    """A flood of tokens naming a dead kid must not flood CyberdyneAuth."""
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    route = respx.get(JWKS_URL).respond(json=jwks_document(key))
    client = make_discovery(http, cooldown=timedelta(seconds=60))
    now = datetime.now(tz=UTC)
    await client.jwks(now)

    for offset in range(1, 10):
        await client.refresh_jwks_for_unknown_kid(now + timedelta(seconds=offset))

    assert route.call_count == 2, "one initial fetch plus one refresh"


@respx.mock
async def test_refresh_resumes_after_the_cooldown(http: httpx.AsyncClient, key: SigningKey) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    route = respx.get(JWKS_URL).respond(json=jwks_document(key))
    client = make_discovery(http, cooldown=timedelta(seconds=60))
    now = datetime.now(tz=UTC)
    await client.jwks(now)
    await client.refresh_jwks_for_unknown_kid(now + timedelta(seconds=1))
    await client.refresh_jwks_for_unknown_kid(now + timedelta(seconds=90))

    assert route.call_count == 3


def test_key_lookup_by_kid(http: httpx.AsyncClient, key: SigningKey) -> None:
    client = make_discovery(http)
    document = jwks_document(key)
    assert client.key_for("key-1", document) is not None
    assert client.key_for("missing", document) is None


def test_key_lookup_without_kid_uses_a_sole_key(http: httpx.AsyncClient, key: SigningKey) -> None:
    client = make_discovery(http)
    assert client.key_for(None, jwks_document(key)) is not None
    assert client.key_for(None, jwks_document(key, SigningKey("key-2"))) is None


def test_malformed_jwks_is_rejected(http: httpx.AsyncClient) -> None:
    with pytest.raises(InvalidTokenError, match="no keys"):
        make_discovery(http).key_for("key-1", {"not-keys": []})


# --- token verification ----------------------------------------------------


def verifier(http: httpx.AsyncClient) -> JwtTokenVerifier:
    return JwtTokenVerifier(make_discovery(http), clock_skew=SKEW)


@respx.mock
async def test_valid_token_is_accepted(http: httpx.AsyncClient, key: SigningKey) -> None:
    mock_auth(respx.mock, key)
    principal = await verifier(http).verify(key.sign(user_claims()))
    assert principal.subject == "user-1"


@respx.mock
async def test_admin_claim_is_carried(http: httpx.AsyncClient, key: SigningKey) -> None:
    mock_auth(respx.mock, key)
    principal = await verifier(http).verify(key.sign(user_claims(is_admin=True)))
    assert principal.is_admin


@respx.mock
async def test_issuer_mismatch_is_rejected(http: httpx.AsyncClient, key: SigningKey) -> None:
    mock_auth(respx.mock, key)
    token = key.sign(user_claims(issuer="cyberdyne-auth"))
    with pytest.raises(InvalidTokenError, match="issuer"):
        await verifier(http).verify(token)


@respx.mock
async def test_expired_token_is_rejected(http: httpx.AsyncClient, key: SigningKey) -> None:
    mock_auth(respx.mock, key)
    token = key.sign(user_claims(expires_in=timedelta(minutes=-30)))
    with pytest.raises(TokenExpiredError):
        await verifier(http).verify(token)


@respx.mock
async def test_token_signed_by_another_key_is_rejected(
    http: httpx.AsyncClient, key: SigningKey
) -> None:
    mock_auth(respx.mock, key)
    impostor = SigningKey("key-1")  # same kid, different key material
    with pytest.raises(InvalidTokenError, match="signature"):
        await verifier(http).verify(impostor.sign(user_claims()))


@respx.mock
async def test_unknown_kid_is_rejected_after_refresh(
    http: httpx.AsyncClient, key: SigningKey
) -> None:
    mock_auth(respx.mock, key)
    stranger = SigningKey("key-unknown")
    with pytest.raises(InvalidTokenError, match="unknown key"):
        await verifier(http).verify(stranger.sign(user_claims()))


@respx.mock
async def test_rotated_key_is_picked_up_by_refresh(
    http: httpx.AsyncClient, key: SigningKey
) -> None:
    """An unknown kid is the normal signal that keys rotated."""
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    rotated = SigningKey("key-2")
    route = respx.get(JWKS_URL)
    route.side_effect = [
        httpx.Response(200, json=jwks_document(key)),
        httpx.Response(200, json=jwks_document(key, rotated)),
    ]

    principal = await verifier(http).verify(rotated.sign(user_claims()))

    assert principal.subject == "user-1"


@respx.mock
async def test_unsigned_token_is_rejected(http: httpx.AsyncClient, key: SigningKey) -> None:
    mock_auth(respx.mock, key)
    unsigned = jwt.encode(user_claims(), key="", algorithm=None)  # type: ignore[arg-type]
    with pytest.raises(InvalidTokenError, match="not permitted"):
        await verifier(http).verify(unsigned)


@respx.mock
async def test_undiscovered_algorithm_is_rejected(http: httpx.AsyncClient, key: SigningKey) -> None:
    """Discovery advertises RS256 only; an HS256 token must not be accepted."""
    mock_auth(respx.mock, key, algorithms=("RS256",))
    hs256 = jwt.encode(user_claims(), key="shared-secret", algorithm="HS256")
    with pytest.raises(InvalidTokenError, match="not advertised"):
        await verifier(http).verify(hs256)


@respx.mock
async def test_garbage_token_is_rejected(http: httpx.AsyncClient, key: SigningKey) -> None:
    mock_auth(respx.mock, key)
    with pytest.raises(InvalidTokenError, match="header"):
        await verifier(http).verify("not-a-jwt")


# --- service tokens --------------------------------------------------------


def service_tokens(http: httpx.AsyncClient) -> ServiceTokenProvider:
    return ServiceTokenProvider(
        make_discovery(http), http, client_id="cyberfs", client_secret="s3cret"
    )


@respx.mock
async def test_service_token_is_requested(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(json={"access_token": "svc-token", "expires_in": 900})

    assert await service_tokens(http).token() == "svc-token"


@respx.mock
async def test_service_token_is_cached_until_near_expiry(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    route = respx.post(TOKEN_URL).respond(json={"access_token": "svc", "expires_in": 900})
    provider = service_tokens(http)
    now = datetime.now(tz=UTC)

    await provider.token(now)
    await provider.token(now + timedelta(seconds=600))

    assert route.call_count == 1


@respx.mock
async def test_service_token_is_renewed_before_it_expires(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    route = respx.post(TOKEN_URL).respond(json={"access_token": "svc", "expires_in": 900})
    provider = service_tokens(http)
    now = datetime.now(tz=UTC)

    await provider.token(now)
    await provider.token(now + timedelta(seconds=860))  # inside the 60s margin

    assert route.call_count == 2


@respx.mock
async def test_service_token_failure_is_a_dependency_error(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(status_code=503)
    with pytest.raises(DependencyUnavailableError):
        await service_tokens(http).token()


@respx.mock
async def test_token_endpoint_without_access_token_is_an_error(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(json={"expires_in": 900})
    with pytest.raises(DependencyUnavailableError, match="access_token"):
        await service_tokens(http).token()


# --- introspection ---------------------------------------------------------


def introspector(http: httpx.AsyncClient) -> TokenIntrospectionClient:
    discovery = make_discovery(http)
    provider = ServiceTokenProvider(discovery, http, client_id="cyberfs", client_secret="s3cret")
    return TokenIntrospectionClient(discovery, provider, http)


@respx.mock
async def test_active_token_yields_a_principal(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(json={"access_token": "svc", "expires_in": 900})
    respx.post(INTROSPECTION_URL).respond(
        json={"active": True, "sub": "user-1", "is_admin": True, "exp": 1893456000}
    )

    principal = await introspector(http).introspect("tok")

    assert principal.subject == "user-1"
    assert principal.is_admin


@respx.mock
async def test_inactive_token_is_rejected(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(json={"access_token": "svc", "expires_in": 900})
    respx.post(INTROSPECTION_URL).respond(json={"active": False})

    with pytest.raises(InvalidTokenError, match="not active"):
        await introspector(http).introspect("tok")


@respx.mock
async def test_introspection_carries_the_service_token(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(json={"access_token": "svc-abc", "expires_in": 900})
    route = respx.post(INTROSPECTION_URL).respond(
        json={"active": True, "sub": "u", "exp": 1893456000}
    )

    await introspector(http).introspect("tok")

    assert route.calls[0].request.headers["authorization"] == "Bearer svc-abc"


@respx.mock
async def test_stale_service_token_is_retried_once(http: httpx.AsyncClient) -> None:
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(json={"access_token": "svc", "expires_in": 900})
    route = respx.post(INTROSPECTION_URL)
    route.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json={"active": True, "sub": "user-1", "exp": 1893456000}),
    ]

    principal = await introspector(http).introspect("tok")

    assert principal.subject == "user-1"
    assert route.call_count == 2


@respx.mock
async def test_introspection_outage_is_a_dependency_error(http: httpx.AsyncClient) -> None:
    """Fail closed -- never fall back to the local claim."""
    respx.get(DISCOVERY_URL).respond(json=discovery_document())
    respx.post(TOKEN_URL).respond(json={"access_token": "svc", "expires_in": 900})
    respx.post(INTROSPECTION_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(DependencyUnavailableError):
        await introspector(http).introspect("tok")


@respx.mock
async def test_missing_introspection_endpoint_is_a_dependency_error(
    http: httpx.AsyncClient,
) -> None:
    document = discovery_document()
    del document["introspection_endpoint"]
    respx.get(DISCOVERY_URL).respond(json=document)

    with pytest.raises(DependencyUnavailableError, match="introspection"):
        await introspector(http).introspect("tok")
