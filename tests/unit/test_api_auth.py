"""Auth dependencies at the HTTP boundary, and AUTH_DEV_MODE."""

from __future__ import annotations

from http import HTTPStatus

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.adapters.inbound.api.dependencies import (
    AdminPrincipal,
    CurrentPrincipal,
    FreshPrincipal,
)
from cyberfs.adapters.outbound.auth.dev_mode import DevModeVerifier, parse_dev_token
from cyberfs.application.authentication import AUTH_FAILURE_WINDOW, AuthenticationService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import DependencyUnavailableError, InvalidTokenError
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.infrastructure.settings import Environment

from .conftest import make_settings
from .test_authentication_service import RecordingAudit, StubIdentity, user


def protected(app: FastAPI) -> None:
    @app.get("/probe/read")
    async def read(principal: CurrentPrincipal) -> dict[str, object]:
        return {"subject": principal.subject}

    @app.get("/probe/grant")
    async def grant(principal: FreshPrincipal) -> dict[str, object]:
        return {"subject": principal.subject}

    @app.get("/probe/admin")
    async def admin(principal: AdminPrincipal) -> dict[str, object]:
        return {"subject": principal.subject, "is_admin": principal.is_admin}


def build_app(
    verifier: StubIdentity,
    introspector: StubIdentity | None = None,
    *,
    limit: int = 30,
) -> FastAPI:
    app = create_app(make_settings())
    app.state.authentication = AuthenticationService(
        verifier=verifier,
        introspector=introspector or StubIdentity(user()),
        audit=RecordingAudit(),
        failure_limiter=FixedWindowLimiter(limit=limit, window=AUTH_FAILURE_WINDOW),
    )
    protected(app)
    return app


def call(app: FastAPI, path: str, token: str | None = "tok") -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get(path, headers=headers)


# --- credentials -----------------------------------------------------------


def test_authenticated_read_succeeds() -> None:
    app = build_app(StubIdentity(user()))
    response = call(app, "/probe/read")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["subject"] == "user-1"


def test_missing_credentials_are_rejected() -> None:
    app = build_app(StubIdentity(user()))
    response = call(app, "/probe/read", token=None)
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_denial_does_not_disclose_the_resource() -> None:
    """A 401 body must not hint at what was behind the endpoint."""
    app = build_app(StubIdentity(error=InvalidTokenError()))
    body = call(app, "/probe/read").json()
    assert set(body) == {"type", "title", "status", "code", "detail", "request_id"}


def test_expired_token_reports_its_code() -> None:
    from cyberfs.domain.errors import TokenExpiredError

    app = build_app(StubIdentity(error=TokenExpiredError()))
    response = call(app, "/probe/read")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "token_expired"


# --- mode separation -------------------------------------------------------


def test_read_route_does_not_introspect() -> None:
    introspector = StubIdentity(user())
    app = build_app(StubIdentity(user()), introspector)
    call(app, "/probe/read")
    assert introspector.calls == 0


def test_grant_route_introspects() -> None:
    introspector = StubIdentity(user())
    app = build_app(StubIdentity(user()), introspector)
    call(app, "/probe/grant")
    assert introspector.calls == 1


def test_admin_route_denies_a_non_admin() -> None:
    app = build_app(StubIdentity(user()), StubIdentity(user()))
    response = call(app, "/probe/admin")
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_admin_route_admits_an_admin() -> None:
    app = build_app(StubIdentity(user(is_admin=True)), StubIdentity(user(is_admin=True)))
    response = call(app, "/probe/admin")
    assert response.status_code == HTTPStatus.OK


def test_demoted_admin_is_denied_immediately() -> None:
    """Claim says admin; the identity plane says otherwise and wins."""
    app = build_app(StubIdentity(user(is_admin=True)), StubIdentity(user(is_admin=False)))
    response = call(app, "/probe/admin")
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_introspection_outage_on_a_fresh_route_is_503_with_retry_after() -> None:
    app = build_app(StubIdentity(user()), StubIdentity(error=DependencyUnavailableError()))
    response = call(app, "/probe/grant")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"]


def test_read_route_still_works_during_an_introspection_outage() -> None:
    """An auth-plane blip must not stop ordinary reads."""
    app = build_app(StubIdentity(user()), StubIdentity(error=DependencyUnavailableError()))
    assert call(app, "/probe/read").status_code == HTTPStatus.OK


# --- rate limiting ---------------------------------------------------------


def test_repeated_failures_return_429_with_retry_after() -> None:
    app = build_app(StubIdentity(error=InvalidTokenError()), limit=2)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = {"Authorization": "Bearer bad"}
        for _ in range(2):
            assert client.get("/probe/read", headers=headers).status_code == 401
        limited = client.get("/probe/read", headers=headers)

    assert limited.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert limited.headers["Retry-After"]
    assert limited.json()["code"] == "rate_limited"


# --- dev mode --------------------------------------------------------------


def test_dev_token_yields_the_named_subject() -> None:
    assert parse_dev_token("dev:alice").subject == "alice"


def test_dev_token_can_grant_admin() -> None:
    principal = parse_dev_token("dev:alice:admin")
    assert principal.subject == "alice"
    assert principal.is_admin


def test_dev_token_without_admin_is_not_admin() -> None:
    assert not parse_dev_token("dev:alice").is_admin


def test_arbitrary_token_falls_back_to_the_default_subject() -> None:
    assert parse_dev_token("whatever").subject == "dev-user"


async def test_dev_verifier_satisfies_both_ports() -> None:
    stub = DevModeVerifier()
    assert isinstance(await stub.verify("dev:alice"), Principal)
    assert isinstance(await stub.introspect("dev:alice"), Principal)


def test_dev_mode_app_serves_without_a_live_auth_service() -> None:
    app = create_app(make_settings(auth_dev_mode=True))
    protected(app)
    with TestClient(app) as client:
        response = client.get("/probe/admin", headers={"Authorization": "Bearer dev:root:admin"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["subject"] == "root"


def test_dev_mode_reports_auth_as_disabled_not_failing() -> None:
    app = create_app(make_settings(auth_dev_mode=True))
    with TestClient(app) as client:
        body = client.get("/health/ready").json()
    auth = next(c for c in body["components"] if c["name"] == "cyberdyne_auth")
    assert auth["status"] == "disabled"
    assert body["status"] == "ready"


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_dev_mode_cannot_be_enabled_in_a_deployed_environment(environment: Environment) -> None:
    """The stub is unreachable in production because settings refuse to load."""
    import base64

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="AUTH_DEV_MODE"):
        make_settings(
            auth_dev_mode=True,
            environment=environment,
            master_key=base64.b64encode(b"\x09" * 32).decode(),
        )
