"""Construct outbound adapters and hand them to the application layer.

Kept out of `app.py` so the factory stays readable as more subsystems land.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import timedelta

import httpx
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine

from cyberfs.adapters.outbound.audit_log import LoggingAuditSink
from cyberfs.adapters.outbound.auth.dev_mode import DevModeVerifier
from cyberfs.adapters.outbound.auth.directory import CyberdyneDirectory
from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient
from cyberfs.adapters.outbound.auth.introspection import TokenIntrospectionClient
from cyberfs.adapters.outbound.auth.service_token import ServiceTokenProvider
from cyberfs.adapters.outbound.auth.verifier import JwtTokenVerifier
from cyberfs.adapters.outbound.cipher import AesGcmContentCipher
from cyberfs.adapters.outbound.crypto import MasterKeyProvider
from cyberfs.adapters.outbound.objects.minio_store import MinioObjectStore
from cyberfs.application.authentication import AUTH_FAILURE_WINDOW, AuthenticationService
from cyberfs.application.content import ContentService
from cyberfs.application.encryption import EncryptionService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.sharing import SharingService
from cyberfs.domain.auth.policy import CacheWindow
from cyberfs.domain.health import ComponentHealth, ComponentStatus, Criticality
from cyberfs.domain.ports.identity import TokenIntrospector, TokenVerifier
from cyberfs.domain.ports.storage import ObjectStore
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.infrastructure.db import ping
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


class DatabaseHealthProbe:
    """Postgres is required: without it CyberFS cannot serve any request."""

    name = "postgres"
    criticality = Criticality.REQUIRED

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> ComponentHealth:
        started = time.perf_counter()
        await ping(self._engine)
        return ComponentHealth(
            name=self.name,
            status=ComponentStatus.UP,
            criticality=self.criticality,
            latency_ms=(time.perf_counter() - started) * 1000,
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


def build_provisioning(settings: Settings, keys: MasterKeyProvider) -> ProvisioningService:
    return ProvisioningService(keys, default_quota_bytes=settings.default_quota_bytes)


def build_object_store(settings: Settings) -> MinioObjectStore:
    return MinioObjectStore(
        Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            secure=settings.minio_secure,
            region=settings.minio_region,
        ),
        settings.minio_bucket,
        part_bytes=settings.upload_chunk_bytes,
    )


def build_key_provider(settings: Settings) -> MasterKeyProvider:
    return MasterKeyProvider(settings.master_key_bytes, previous=settings.master_key_previous_bytes)


def build_encryption(settings: Settings, keys: MasterKeyProvider) -> EncryptionService:
    return EncryptionService(
        keys,
        AesGcmContentCipher(settings.encryption_frame_bytes),
        encryption_default_on=settings.encryption_default_on,
    )


def build_content(
    settings: Settings, objects: ObjectStore, encryption: EncryptionService
) -> ContentService:
    return ContentService(
        objects,
        max_upload_bytes=settings.max_upload_bytes,
        upload_chunk_bytes=settings.upload_chunk_bytes,
        version_retention_count=settings.version_retention_count,
        encryption=encryption,
    )


class ObjectStoreHealthProbe:
    """MinIO is required: without it no content can be read or written."""

    name = "minio"
    criticality = Criticality.REQUIRED

    def __init__(self, store: MinioObjectStore) -> None:
        self._store = store

    async def check(self) -> ComponentHealth:
        started = time.perf_counter()
        await self._store.stat("__healthcheck__")
        return ComponentHealth(
            name=self.name,
            status=ComponentStatus.UP,
            criticality=self.criticality,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def build_sharing(
    settings: Settings, http: httpx.AsyncClient, encryption: EncryptionService
) -> SharingService:
    """Wire the recipient directory, or a stub when auth is stubbed."""
    if settings.auth_dev_mode:
        return SharingService(
            LocalOnlyDirectory(),
            keys=encryption,
            passphrase_attempts_per_min=settings.public_link_max_attempts_per_min,
        )
    discovery = build_discovery(settings, http)
    service_tokens = ServiceTokenProvider(
        discovery,
        http,
        client_id=settings.cyberfs_client_id,
        client_secret=settings.cyberfs_client_secret.get_secret_value(),
    )
    return SharingService(
        CyberdyneDirectory(settings.cyberdyne_auth_base_url, service_tokens, http),
        keys=encryption,
        passphrase_attempts_per_min=settings.public_link_max_attempts_per_min,
    )


class LocalOnlyDirectory:
    """Resolves subjects only, for local development without CyberdyneAuth.

    Sharing by email needs the real org directory; under `AUTH_DEV_MODE` any
    identifier is taken at face value as a subject.
    """

    async def find_subject(self, identifier: str, *, within_orgs: Sequence[str] = ()) -> str | None:
        return identifier.strip() or None


class EncryptionHealthProbe:
    """Reports whether the configured master key opens what is stored.

    A key that cannot unwrap existing material must take the replica out of
    rotation, not surface as a 500 on every encrypted file.
    """

    name = "encryption"
    criticality = Criticality.REQUIRED

    def __init__(self, encryption: EncryptionService, unit_of_work: object) -> None:
        self._encryption = encryption
        self._unit_of_work = unit_of_work

    async def check(self) -> ComponentHealth:
        started = time.perf_counter()
        async with self._unit_of_work() as uow:  # type: ignore[operator]
            usable = await self._encryption.verify_master_key(uow)
        return ComponentHealth(
            name=self.name,
            status=ComponentStatus.UP if usable else ComponentStatus.DOWN,
            criticality=self.criticality,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=None if usable else "master key cannot open stored key material",
        )
