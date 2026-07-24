"""Construct outbound adapters and hand them to the application layer.

Kept out of `app.py` so the factory stays readable as more subsystems land.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from cyberfs.adapters.outbound.audit_log import LoggingAuditSink
from cyberfs.adapters.outbound.auth.dev_mode import DevModeVerifier
from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient
from cyberfs.adapters.outbound.auth.introspection import TokenIntrospectionClient
from cyberfs.adapters.outbound.auth.service_token import ServiceTokenProvider
from cyberfs.adapters.outbound.auth.verifier import JwtTokenVerifier
from cyberfs.application.authentication import AUTH_FAILURE_WINDOW, AuthenticationService
from cyberfs.domain.auth.policy import CacheWindow
from cyberfs.domain.health import ComponentHealth, ComponentStatus, Criticality
from cyberfs.domain.ports.identity import TokenIntrospector, TokenVerifier
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.infrastructure.settings import Settings

HTTP_TIMEOUT_SECONDS = 10.0


def build_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": f"CyberFS/{settings.environment}"},
    )


def build_discovery(settings: Settings, http: httpx.AsyncClient) -> DiscoveryClient:
    return DiscoveryClient(
        settings.cyberdyne_auth_base_url,
        http,
        discovery_window=CacheWindow(
            ttl=timedelta(seconds=settings.oidc_discovery_ttl_seconds),
            stale_max=timedelta(seconds=settings.jwks_stale_max_seconds),
        ),
        jwks_window=CacheWindow(
            ttl=timedelta(seconds=settings.cache_ttl_jwks_seconds),
            stale_max=timedelta(seconds=settings.jwks_stale_max_seconds),
        ),
        refresh_cooldown=timedelta(seconds=settings.jwks_refresh_cooldown_seconds),
    )


def build_identity(
    settings: Settings, http: httpx.AsyncClient
) -> tuple[TokenVerifier, TokenIntrospector, DiscoveryClient | None]:
    """Real CyberdyneAuth clients, or the local stub.

    `AUTH_DEV_MODE` cannot be set outside local/test -- the settings validator
    rejects it -- so this branch cannot select the stub in a deployment.
    """
    if settings.auth_dev_mode:
        stub = DevModeVerifier()
        return stub, stub, None

    discovery = build_discovery(settings, http)
    verifier = JwtTokenVerifier(
        discovery, clock_skew=timedelta(seconds=settings.token_clock_skew_seconds)
    )
    service_tokens = ServiceTokenProvider(
        discovery,
        http,
        client_id=settings.cyberfs_client_id,
        client_secret=settings.cyberfs_client_secret.get_secret_value(),
    )
    introspector = TokenIntrospectionClient(discovery, service_tokens, http)
    return verifier, introspector, discovery


def build_authentication(
    settings: Settings,
    verifier: TokenVerifier,
    introspector: TokenIntrospector,
) -> AuthenticationService:
    return AuthenticationService(
        verifier=verifier,
        introspector=introspector,
        audit=LoggingAuditSink(),
        failure_limiter=FixedWindowLimiter(
            limit=settings.ratelimit_auth_failures_per_min,
            window=AUTH_FAILURE_WINDOW,
        ),
    )


class AuthHealthProbe:
    """Reports whether tokens can still be verified.

    Down only when discovery is unreachable *and* no usable cached JWKS
    remains -- a brief CyberdyneAuth blip with a warm cache is not an outage
    for CyberFS.
    """

    name = "cyberdyne_auth"
    criticality = Criticality.REQUIRED

    def __init__(self, discovery: DiscoveryClient | None) -> None:
        self._discovery = discovery

    async def check(self) -> ComponentHealth:
        if self._discovery is None:
            return ComponentHealth(
                name=self.name,
                status=ComponentStatus.DISABLED,
                criticality=Criticality.OPTIONAL,
                detail="AUTH_DEV_MODE",
            )
        await self._discovery.jwks()
        return ComponentHealth(
            name=self.name, status=ComponentStatus.UP, criticality=self.criticality
        )
