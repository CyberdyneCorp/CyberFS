"""App wiring: health endpoints, problem details, correlation, metrics access."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.errors import PROBLEM_JSON, status_for
from cyberfs.adapters.inbound.api.middleware import REQUEST_ID_HEADER
from cyberfs.application.health import HealthService
from cyberfs.domain import errors as err
from cyberfs.domain.health import ComponentStatus, Criticality

from .conftest import make_settings
from .test_health import StubProbe

# --- health ----------------------------------------------------------------


def test_liveness_is_ok(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "alive"


def test_liveness_ignores_a_dead_dependency(app: FastAPI, client: TestClient) -> None:
    """A Postgres outage must not make the orchestrator restart-loop us."""
    app.state.health.register(StubProbe("postgres", ComponentStatus.DOWN))
    assert client.get("/health/live").status_code == HTTPStatus.OK


def test_readiness_ok_when_everything_is_up(app: FastAPI, client: TestClient) -> None:
    app.state.health = HealthService([StubProbe("postgres"), StubProbe("minio")])
    response = client.get("/health/ready")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "ready"


def test_readiness_503_when_postgres_is_down(app: FastAPI, client: TestClient) -> None:
    app.state.health = HealthService([StubProbe("postgres", ComponentStatus.DOWN)])
    response = client.get("/health/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["status"] == "not_ready"


def test_readiness_200_degraded_when_only_cache_is_down(app: FastAPI, client: TestClient) -> None:
    app.state.health = HealthService(
        [
            StubProbe("postgres"),
            StubProbe("cache", ComponentStatus.DOWN, Criticality.OPTIONAL),
        ]
    )
    response = client.get("/health/ready")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "degraded"


def test_readiness_names_the_failing_component(app: FastAPI, client: TestClient) -> None:
    app.state.health = HealthService([StubProbe("minio", ComponentStatus.DOWN)])
    body = client.get("/health/ready").json()
    failing = [c for c in body["components"] if c["status"] == "down"]
    assert [c["name"] for c in failing] == ["minio"]


# --- request correlation ---------------------------------------------------


def test_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health/live", headers={REQUEST_ID_HEADER: "req-abc"})
    assert response.headers[REQUEST_ID_HEADER] == "req-abc"


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    assert client.get("/health/live").headers[REQUEST_ID_HEADER]


# --- problem details -------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (err.TokenExpiredError(), HTTPStatus.UNAUTHORIZED),
        (err.InvalidTokenError(), HTTPStatus.UNAUTHORIZED),
        (err.AuthenticationError(), HTTPStatus.UNAUTHORIZED),
        (err.PermissionDeniedError(), HTTPStatus.FORBIDDEN),
        (err.NotFoundError(), HTTPStatus.NOT_FOUND),
        (err.RecipientUnknownError(), HTTPStatus.NOT_FOUND),
        (err.NameTakenError(), HTTPStatus.CONFLICT),
        (err.WouldCreateCycleError(), HTTPStatus.CONFLICT),
        (err.CrossOwnerMoveError(), HTTPStatus.CONFLICT),
        (err.CannotRevokeOwnerError(), HTTPStatus.CONFLICT),
        (err.CannotShareWithSelfError(), HTTPStatus.CONFLICT),
        (err.PreconditionFailedError(), HTTPStatus.PRECONDITION_FAILED),
        (err.PayloadTooLargeError(), HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
        (err.ValidationError(), HTTPStatus.UNPROCESSABLE_ENTITY),
        (err.QuotaExceededError(), HTTPStatus.INSUFFICIENT_STORAGE),
        (err.RateLimitedError(), HTTPStatus.TOO_MANY_REQUESTS),
        (err.DependencyUnavailableError(), HTTPStatus.SERVICE_UNAVAILABLE),
        (err.IntegrityFailureError(), HTTPStatus.INTERNAL_SERVER_ERROR),
        (err.KeyUnavailableError(), HTTPStatus.INTERNAL_SERVER_ERROR),
        (err.CyberFSError(), HTTPStatus.INTERNAL_SERVER_ERROR),
    ],
)
def test_domain_errors_map_to_status(error: err.CyberFSError, expected: HTTPStatus) -> None:
    assert status_for(error) == expected


def test_domain_error_renders_problem_details(app: FastAPI, client: TestClient) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise err.NameTakenError("a sibling already uses that name")

    response = client.get("/boom")
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["code"] == "name_taken"
    assert body["status"] == int(HTTPStatus.CONFLICT)
    assert body["detail"] == "a sibling already uses that name"
    assert body["request_id"]


def test_unexpected_error_does_not_leak_its_message(app: FastAPI) -> None:
    """An unexpected exception may quote content; only a generic body escapes."""

    @app.get("/kaboom")
    async def kaboom() -> None:
        raise RuntimeError("secret content fragment: hunter2")

    with TestClient(app, raise_server_exceptions=False) as local:
        response = local.get("/kaboom")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "hunter2" not in response.text
    assert response.json()["code"] == "internal_error"


def test_integrity_failure_body_carries_no_crypto_detail(app: FastAPI, client: TestClient) -> None:
    """`content-encryption/spec.md`: no nonces, tags, or ciphertext in errors."""

    @app.get("/tampered")
    async def tampered() -> None:
        raise err.IntegrityFailureError(node_id="n-1")

    body = client.get("/tampered").json()
    assert body["code"] == "integrity_failure"
    assert set(body) == {"type", "title", "status", "code", "detail", "request_id"}


# --- metrics ---------------------------------------------------------------


def test_metrics_served_to_an_internal_client(app: FastAPI) -> None:
    with TestClient(app, client=("10.0.4.9", 51234)) as internal:
        response = internal.get("/metrics")
    assert response.status_code == HTTPStatus.OK
    assert "cyberfs_http_requests_total" in response.text


@pytest.mark.parametrize(
    "peer",
    [
        "203.0.113.7",  # TEST-NET-3: not globally routable, but not ours either
        "8.8.8.8",
        "192.0.2.1",  # TEST-NET-1
        "testclient",  # a hostname is not an address
    ],
)
def test_metrics_refused_from_outside(app: FastAPI, peer: str) -> None:
    with TestClient(app, client=(peer, 51234)) as external:
        assert external.get("/metrics").status_code == HTTPStatus.NOT_FOUND


def test_metrics_endpoint_absent_when_disabled() -> None:
    app = create_app_with(metrics_enabled=False)
    with TestClient(app) as local:
        assert local.get("/metrics").status_code == HTTPStatus.NOT_FOUND


def create_app_with(**overrides: object) -> FastAPI:
    from cyberfs.adapters.inbound.api.app import create_app

    return create_app(make_settings(**overrides))
