"""FastAPI application factory -- the composition root.

Adapters are constructed here and handed to the application layer as ports.
Dependency probes register themselves with `HealthService` as each adapter
comes online in later phases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cyberfs import __version__
from cyberfs.adapters.inbound.api import health, metrics
from cyberfs.adapters.inbound.api.composition import (
    AuthHealthProbe,
    build_authentication,
    build_http_client,
    build_identity,
)
from cyberfs.adapters.inbound.api.errors import register_error_handlers
from cyberfs.adapters.inbound.api.middleware import RequestContextMiddleware
from cyberfs.application.health import HealthService
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
    try:
        yield
    finally:
        await app.state.http.aclose()
        logger.info("stopping", version=__version__)


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

    app.state.http = build_http_client(settings)
    verifier, introspector, discovery = build_identity(settings, app.state.http)
    app.state.authentication = build_authentication(settings, verifier, introspector)
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
    if settings.metrics_enabled:
        app.include_router(metrics.router)

    return app
