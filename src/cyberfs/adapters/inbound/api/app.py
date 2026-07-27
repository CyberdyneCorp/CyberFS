"""FastAPI application factory -- the composition root.

Adapters are constructed here and handed to the application layer as ports.
Dependency probes register themselves with `HealthService` as each adapter
comes online in later phases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cyberfs import __version__
from cyberfs.adapters.inbound.api import health, metrics
from cyberfs.adapters.inbound.api.composition import (
    AuthHealthProbe,
    CacheHealthProbe,
    DatabaseHealthProbe,
    EncryptionHealthProbe,
    ObjectStoreHealthProbe,
    build_authentication,
    build_backup,
    build_cache,
    build_content,
    build_encryption,
    build_http_client,
    build_identity,
    build_key_provider,
    build_object_store,
    build_provisioning,
    build_rewrap,
    build_s3_authenticator,
    build_sharing,
    disabled_backup_probe,
)
from cyberfs.adapters.inbound.api.errors import register_error_handlers
from cyberfs.adapters.inbound.api.middleware import RequestContextMiddleware
from cyberfs.adapters.inbound.api.routers import admin as admin_router
from cyberfs.adapters.inbound.api.routers import content as content_router
from cyberfs.adapters.inbound.api.routers import me as me_router
from cyberfs.adapters.inbound.api.routers import nodes as nodes_router
from cyberfs.adapters.inbound.api.routers import s3 as s3_router
from cyberfs.adapters.inbound.api.routers import s3_keys as s3_keys_router
from cyberfs.adapters.inbound.api.routers import shares as shares_router
from cyberfs.adapters.inbound.api.routers import webdav as webdav_router
from cyberfs.adapters.outbound.db.unit_of_work import SqlUnitOfWork
from cyberfs.application.activity import ActivityService
from cyberfs.application.admin import AdminService
from cyberfs.application.health import HealthService
from cyberfs.application.jobs import ActivityPruneJob
from cyberfs.application.nodes import NodeService
from cyberfs.application.s3_auth import S3SignatureVerifier
from cyberfs.application.s3_keys import S3AccessKeyService
from cyberfs.application.s3_objects import S3ObjectService
from cyberfs.application.s3_presign import S3PresignService
from cyberfs.application.webdav_auth import WebDavAuthenticator
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.infrastructure.db import create_engine, create_session_factory
from cyberfs.infrastructure.logging import configure_logging, get_logger
from cyberfs.infrastructure.settings import Environment, Settings, get_settings

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "starting",
        version=__version__,
        environment=str(settings.environment),
        encryption_default_on=settings.encryption_default_on,
        auth_dev_mode=settings.auth_dev_mode,
    )
    await _provision_bucket(app)
    scheduler = app.state.backup_scheduler
    if scheduler is not None:
        await scheduler.start()
        logger.info("backup_scheduler_started", cron=settings.backup_cron)
    await app.state.rewrap_scheduler.start()
    logger.info("rewrap_scheduler_started", cron=settings.rewrap_cron)
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        await app.state.rewrap_scheduler.stop()
        await app.state.http.aclose()
        await app.state.engine.dispose()
        logger.info("stopping", version=__version__)


def _wire_backup(app: FastAPI, settings: Settings) -> None:
    """Assemble the backup subsystem and register its health probe.

    The scheduler is only *started* in the lifespan (it needs a running loop);
    here it is merely constructed. When backups are disabled the probe is
    registered in a DISABLED state and nothing is scheduled -- the clean no-op
    `backup-restore/spec.md` requires.
    """
    wiring = build_backup(
        settings,
        engine=app.state.engine,
        source_store=app.state.objects,
        session_factory=app.state.session_factory,
        jobs=app.state.admin.jobs,
    )
    if wiring is None:
        app.state.backup = None
        app.state.backup_job = None
        app.state.backup_scheduler = None
        app.state.health.register(disabled_backup_probe(settings))
        return
    app.state.backup = wiring.service
    app.state.backup_job = wiring.job
    app.state.backup_scheduler = wiring.scheduler
    app.state.health.register(wiring.probe)


async def _provision_bucket(app: FastAPI) -> None:
    """Create the content bucket if absent.

    Deliberately non-fatal: `deployment/spec.md` requires liveness to be
    independent of dependencies, so a MinIO blip at boot must not stop the
    process. Readiness reports the outage, and the replica stays out of
    rotation until the store is reachable.
    """
    try:
        await app.state.objects.ensure_bucket()
    except Exception as exc:
        logger.error("bucket_provisioning_failed", error=type(exc).__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.environment is not Environment.LOCAL,
    )

    app = FastAPI(
        title="CyberFS",
        version=__version__,
        description="Backend filesystem service with sharing and optional content encryption.",
        root_path=settings.api_root_path,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.health = HealthService()

    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.unit_of_work = lambda: SqlUnitOfWork(app.state.session_factory)
    app.state.health.register(DatabaseHealthProbe(app.state.engine))

    app.state.http = build_http_client(settings)
    verifier, introspector, discovery = build_identity(settings, app.state.http)
    app.state.authentication = build_authentication(settings, verifier, introspector)
    app.state.keys = build_key_provider(settings)
    app.state.encryption = build_encryption(settings, app.state.keys)
    app.state.provisioning = build_provisioning(settings, app.state.keys)
    app.state.cache = build_cache(settings)
    app.state.health.register(CacheHealthProbe(app.state.cache))
    app.state.nodes = NodeService(
        max_tree_depth=settings.max_tree_depth,
        page_size_max=settings.page_size_max,
        cache=app.state.cache,
    )
    app.state.objects = build_object_store(settings)
    app.state.content = build_content(settings, app.state.objects, app.state.encryption)
    app.state.health.register(ObjectStoreHealthProbe(app.state.objects))
    app.state.admin = AdminService(show_filenames=settings.admin_show_filenames)
    app.state.activity = ActivityService(
        max_window_days=settings.activity_max_window_days,
        retention_days=settings.activity_retention_days,
        page_size_max=settings.page_size_max,
    )
    app.state.activity_prune_job = ActivityPruneJob(settings)
    app.state.s3_keys = S3AccessKeyService(keys=app.state.keys)
    app.state.webdav_authentication = WebDavAuthenticator(app.state.keys)
    # Signature verification lands now (phase 4); the S3 HTTP surface that
    # feeds it a parsed request mounts in a later phase.
    app.state.s3_authenticator = S3SignatureVerifier(
        keys=app.state.keys,
        failure_limiter=FixedWindowLimiter(
            limit=settings.ratelimit_auth_failures_per_min,
            window=timedelta(minutes=1),
        ),
        region=settings.s3_region,
        clock_skew_seconds=settings.s3_clock_skew_seconds,
    )
    # The credential-resolution entry: SigV4 or bearer to a `Principal`, reusing
    # the REST freshness path. The S3 HTTP routes that call it mount in phase 6.
    app.state.s3_authentication = build_s3_authenticator(
        app.state.s3_authenticator, app.state.authentication
    )
    # The S3 object surface orchestrates reads and writes over the existing
    # content and node services. Built unconditionally; only the HTTP routes
    # below are gated on `S3_API_ENABLED`.
    app.state.s3_objects = S3ObjectService(
        app.state.nodes,
        app.state.content,
        app.state.objects,
        page_size_max=settings.page_size_max,
    )
    # Presigned-URL issuance, rooted at CyberFS's own S3 endpoint and bound to a
    # CyberFS access key -- never the object store. Only built when a public
    # endpoint is configured, so a URL is never minted without one to root it at.
    app.state.s3_public_endpoint = settings.s3_public_endpoint
    app.state.s3_presign = (
        S3PresignService(
            app.state.keys,
            endpoint=settings.s3_public_endpoint,
            base_path=settings.s3_base_path,
            region=settings.s3_region,
        )
        if settings.s3_public_endpoint
        else None
    )
    _wire_backup(app, settings)
    app.state.sharing = build_sharing(
        settings, app.state.http, app.state.encryption, app.state.cache
    )
    rewrap_wiring = build_rewrap(
        settings,
        encryption=app.state.encryption,
        cache=app.state.cache,
        unit_of_work=app.state.unit_of_work,
        jobs=app.state.admin.jobs,
    )
    app.state.rewrap_job = rewrap_wiring.job
    app.state.rewrap_scheduler = rewrap_wiring.scheduler
    app.state.health.register(EncryptionHealthProbe(app.state.encryption, app.state.unit_of_work))
    app.state.health.register(AuthHealthProbe(discovery))

    # Outermost first: correlation wraps metrics so failed requests are still
    # attributable to a request id.
    app.add_middleware(RequestContextMiddleware)
    if settings.metrics_enabled:
        app.add_middleware(metrics.MetricsMiddleware)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(nodes_router.router)
    app.include_router(content_router.router)
    app.include_router(shares_router.router)
    app.include_router(me_router.router)
    app.include_router(s3_keys_router.router)
    app.include_router(admin_router.router)
    # The S3-compatible surface is mounted only when enabled -- a deployment
    # without it exposes no such routes at all.
    # Mounted by default, unlike S3. `webdav_requires_tls` refuses plaintext
    # outside local/test, which is what makes a default-on Basic surface safe.
    if settings.webdav_enabled:
        app.include_router(
            webdav_router.build_webdav_router(
                base_path=settings.webdav_base_path,
                requires_tls=settings.webdav_requires_tls,
            )
        )
    if settings.s3_api_enabled:
        app.include_router(s3_router.create_s3_router(settings.s3_base_path))
    if settings.metrics_enabled:
        app.include_router(metrics.router)

    return app
