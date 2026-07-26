"""Construct outbound adapters and hand them to the application layer.

Kept out of `app.py` so the factory stays readable as more subsystems land.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import redis.asyncio as aioredis
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cyberfs.adapters.outbound.audit_log import LoggingAuditSink
from cyberfs.adapters.outbound.auth.dev_mode import DevModeVerifier
from cyberfs.adapters.outbound.auth.directory import CyberdyneDirectory
from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient
from cyberfs.adapters.outbound.auth.introspection import TokenIntrospectionClient
from cyberfs.adapters.outbound.auth.service_token import ServiceTokenProvider
from cyberfs.adapters.outbound.auth.verifier import JwtTokenVerifier
from cyberfs.adapters.outbound.backup.pg_dump import PgDumpMetadataDump
from cyberfs.adapters.outbound.cache.redis_cache import RedisCache
from cyberfs.adapters.outbound.cipher import AesGcmContentCipher
from cyberfs.adapters.outbound.crypto import MasterKeyProvider
from cyberfs.adapters.outbound.db.backup_repository import SqlBackupRepository
from cyberfs.adapters.outbound.objects.minio_store import MinioObjectStore
from cyberfs.application.admin import JobStatusRegistry
from cyberfs.application.authentication import AUTH_FAILURE_WINDOW, AuthenticationService
from cyberfs.application.backup import BackupService
from cyberfs.application.caching import CacheService
from cyberfs.application.content import ContentService
from cyberfs.application.encryption import EncryptionService
from cyberfs.application.jobs import RewrapJob
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.s3_auth import S3SignatureVerifier
from cyberfs.application.s3_authentication import S3Authenticator
from cyberfs.application.sharing import SharingService
from cyberfs.domain.auth.policy import CacheWindow, utcnow
from cyberfs.domain.backup import BackupRecord, is_stale
from cyberfs.domain.cache import CachePolicy
from cyberfs.domain.health import ComponentHealth, ComponentStatus, Criticality
from cyberfs.domain.ports.backup import BackupRepository
from cyberfs.domain.ports.identity import (
    SubjectIntrospector,
    TokenIntrospector,
    TokenVerifier,
)
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.ports.storage import ObjectStore
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.infrastructure import metrics
from cyberfs.infrastructure.db import ping
from cyberfs.infrastructure.scheduler import CronScheduler
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


def build_s3_authenticator(
    verifier: S3SignatureVerifier,
    authentication: AuthenticationService,
    subject_introspector: SubjectIntrospector | None = None,
) -> S3Authenticator:
    """The S3 credential-resolution entry: SigV4 or bearer to a `Principal`.

    Composes the phase-4 signature verifier with the REST authentication
    service so both credentials reach the same identity and the same freshness
    path. `subject_introspector` supplies freshness for the tokenless SigV4
    path -- a key-authenticated grant/revocation/transfer verifies the owning
    subject at CyberdyneAuth and fails closed on an outage; absent it, such a
    request fails closed rather than proceeding on the key alone. The S3 HTTP
    routes that call it (and the fresh operations that need this introspector)
    mount in a later phase.
    """
    return S3Authenticator(verifier, authentication, subject_introspector)


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
    settings: Settings,
    http: httpx.AsyncClient,
    encryption: EncryptionService,
    cache: CacheService,
) -> SharingService:
    """Wire the recipient directory, or a stub when auth is stubbed."""
    if settings.auth_dev_mode:
        return SharingService(
            LocalOnlyDirectory(),
            keys=encryption,
            cache=cache,
            passphrase_attempts_per_min=settings.public_link_max_attempts_per_min,
            async_rewrap_threshold_nodes=settings.async_rewrap_threshold_nodes,
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
        cache=cache,
        passphrase_attempts_per_min=settings.public_link_max_attempts_per_min,
        async_rewrap_threshold_nodes=settings.async_rewrap_threshold_nodes,
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


def build_cache(settings: Settings) -> CacheService:
    """Redis, or a null cache that makes every read a miss."""
    client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    cache = RedisCache(
        client,
        operation_timeout=timedelta(milliseconds=settings.cache_op_timeout_ms),
        circuit_trip_after=timedelta(seconds=settings.cache_circuit_trip_seconds),
    )
    return CacheService(
        cache,
        schema_version=settings.cache_schema_version,
        policy=CachePolicy(
            permission=timedelta(seconds=settings.cache_ttl_permission_seconds),
            metadata=timedelta(seconds=settings.cache_ttl_metadata_seconds),
            listing=timedelta(seconds=settings.cache_ttl_listing_seconds),
            jwks=timedelta(seconds=settings.cache_ttl_jwks_seconds),
            quota=timedelta(seconds=settings.cache_ttl_quota_seconds),
            admin_stats=timedelta(seconds=settings.cache_ttl_admin_stats_seconds),
        ),
    )


class CacheHealthProbe:
    """Optional by design: Redis down means slower, not broken.

    `caching/spec.md` requires readiness to stay healthy-but-degraded rather
    than taking the replica out of rotation.
    """

    name = "cache"
    criticality = Criticality.OPTIONAL

    def __init__(self, cache: CacheService) -> None:
        self._cache = cache

    async def check(self) -> ComponentHealth:
        started = time.perf_counter()
        stats = await self._cache.stats()
        usable = bool(stats.get("available"))
        return ComponentHealth(
            name=self.name,
            status=ComponentStatus.UP if usable else ComponentStatus.DOWN,
            criticality=self.criticality,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=None if usable else "cache unreachable; serving from Postgres",
        )


# --- backup ----------------------------------------------------------------


def build_backup_object_store(settings: Settings) -> MinioObjectStore:
    """A second store pointed at `BACKUP_S3_*`, distinct from the primary.

    Startup validation (`Settings._validate_backup_target`) has already refused
    a target identical to the primary endpoint+bucket, so this cannot be aimed
    back at the content bucket by accident.
    """
    if (
        settings.backup_s3_endpoint is None
        or settings.backup_s3_access_key is None
        or settings.backup_s3_secret_key is None
        or settings.backup_s3_bucket is None
    ):
        raise ValueError("BACKUP_S3_* must be configured when backups are enabled")
    return MinioObjectStore(
        Minio(
            settings.backup_s3_endpoint,
            access_key=settings.backup_s3_access_key.get_secret_value(),
            secret_key=settings.backup_s3_secret_key.get_secret_value(),
            # Not `minio_secure`: an off-site target is usually TLS while the
            # primary is plaintext on an internal network.
            secure=settings.backup_target_secure,
            region=settings.minio_region,
        ),
        settings.backup_s3_bucket,
        part_bytes=settings.upload_chunk_bytes,
    )


def build_backup_service(
    settings: Settings,
    *,
    engine: AsyncEngine,
    source_store: ObjectStore,
    repository: BackupRepository,
) -> BackupService:
    return BackupService(
        metadata_dump=PgDumpMetadataDump(engine, settings.database_url),
        source_store=source_store,
        backup_store=build_backup_object_store(settings),
        repository=repository,
        keep_daily=settings.backup_keep_daily,
        keep_weekly=settings.backup_keep_weekly,
        keep_monthly=settings.backup_keep_monthly,
        failed_grace_hours=settings.backup_failed_grace_hours,
        verify_sample_count=settings.backup_verify_sample_count,
        history_days=settings.backup_history_days,
    )


def _emit_backup_metrics(record: BackupRecord) -> None:
    """Translate a finished backup record into the Prometheus instruments."""
    outcome = "verified" if record.is_verified else "failed"
    metrics.backup_runs_total.labels(outcome=outcome).inc()
    if record.duration_seconds is not None:
        metrics.backup_duration_seconds.observe(record.duration_seconds)
    if record.is_verified:
        metrics.backup_bytes_total.inc(record.total_bytes)
        if record.finished_at is not None:
            metrics.backup_last_success_timestamp.set(record.finished_at.timestamp())


class BackupJob:
    """One scheduled (or manually triggered) backup, with its side effects.

    Wraps `BackupService.run` so the pipeline itself stays free of metrics and
    the job registry: this records the outcome for the operations view, emits
    the metrics, prunes on success, and remembers the last record so the manual
    trigger endpoint can return it.
    """

    def __init__(self, service: BackupService, jobs: JobStatusRegistry) -> None:
        self._service = service
        self._jobs = jobs
        self.last_record: BackupRecord | None = None

    async def run(self) -> None:
        record = await self._service.run()
        self.last_record = record
        self._jobs.record(
            "backup",
            outcome="success" if record.is_verified else "failed",
            duration_seconds=record.duration_seconds or 0.0,
            detail=record.error,
        )
        _emit_backup_metrics(record)
        if record.is_verified:
            await self._service.apply_retention()

    async def on_skip(self) -> None:
        """A run that fired while another was in flight -- recorded, not started."""
        metrics.backup_runs_total.labels(outcome="skipped").inc()
        self._jobs.record("backup", outcome="skipped", duration_seconds=0.0, detail="overlap")


class BackupHealthProbe:
    """Backup freshness for the operations view.

    Optional by design: a stale or failed backup degrades readiness and raises
    an alert-level condition, but does not take the replica out of rotation.
    When `BACKUP_ENABLED` is false it reports DISABLED -- a deliberately
    switched-off backup is not a fault (`backup-restore/spec.md`).
    """

    name = "backup"
    criticality = Criticality.OPTIONAL

    def __init__(
        self,
        repository: BackupRepository | None,
        *,
        enabled: bool,
        max_age_hours: int,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repository = repository
        self._enabled = enabled
        self._max_age_hours = max_age_hours
        self._clock = clock

    async def check(self) -> ComponentHealth:
        if not self._enabled or self._repository is None:
            return ComponentHealth(
                name=self.name,
                status=ComponentStatus.DISABLED,
                criticality=self.criticality,
                detail="BACKUP_ENABLED=false",
            )
        latest = await self._repository.latest_verified()
        last_at = latest.finished_at if latest is not None else None
        stale = is_stale(last_at, max_age_hours=self._max_age_hours, now=self._clock())
        return ComponentHealth(
            name=self.name,
            status=ComponentStatus.DOWN if stale else ComponentStatus.UP,
            criticality=self.criticality,
            detail=self._detail(latest, stale),
        )

    def _detail(self, latest: BackupRecord | None, stale: bool) -> str | None:
        if latest is None:
            return f"no verified backup within {self._max_age_hours}h"
        if stale:
            return f"last verified backup at {latest.finished_at:%Y-%m-%dT%H:%M:%SZ} is stale"
        return None


@dataclass(frozen=True, slots=True)
class BackupWiring:
    """The backup subsystem, assembled once so `app.py` can start/stop it."""

    service: BackupService
    scheduler: CronScheduler
    job: BackupJob
    probe: BackupHealthProbe


def build_backup(
    settings: Settings,
    *,
    engine: AsyncEngine,
    source_store: ObjectStore,
    session_factory: async_sessionmaker[AsyncSession],
    jobs: JobStatusRegistry,
) -> BackupWiring | None:
    """Assemble the backup subsystem, or None when backups are disabled.

    When disabled, the caller registers `disabled_backup_probe` and starts
    nothing -- the clean no-op `backup-restore/spec.md` requires.
    """
    if not settings.backup_enabled:
        return None
    repository = SqlBackupRepository(session_factory)
    service = build_backup_service(
        settings, engine=engine, source_store=source_store, repository=repository
    )
    job = BackupJob(service, jobs)
    scheduler = CronScheduler(settings.backup_cron, job.run, name="backup", on_skip=job.on_skip)
    probe = BackupHealthProbe(repository, enabled=True, max_age_hours=settings.backup_max_age_hours)
    return BackupWiring(service=service, scheduler=scheduler, job=job, probe=probe)


def disabled_backup_probe(settings: Settings) -> BackupHealthProbe:
    return BackupHealthProbe(None, enabled=False, max_age_hours=settings.backup_max_age_hours)


# --- rewrap ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RewrapWiring:
    """The async-rewrap worker and its scheduler, assembled for `app.py`."""

    job: RewrapJob
    scheduler: CronScheduler


def build_rewrap(
    settings: Settings,
    *,
    encryption: EncryptionService,
    cache: CacheService,
    unit_of_work: Callable[[], UnitOfWork],
    jobs: JobStatusRegistry,
) -> RewrapWiring:
    """Assemble the worker that completes deferred rewraps of large shares.

    The scheduler is merely constructed here; `app.py` starts it in the lifespan
    once a loop is running. Each run opens a fresh Unit of Work and records its
    outcome in the job registry, so the operational view reports it like any
    other job.
    """
    job = RewrapJob(encryption, cache, settings)

    async def _run() -> None:
        with jobs.timer(job.name):
            async with unit_of_work() as uow:
                await job.run(uow)

    scheduler = CronScheduler(settings.rewrap_cron, _run, name=job.name)
    return RewrapWiring(job=job, scheduler=scheduler)
