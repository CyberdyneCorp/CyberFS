"""Metrics access control and instrument surface."""

from __future__ import annotations

import pytest

from cyberfs.infrastructure.metrics import (
    REGISTRY,
    http_request_duration_seconds,
    http_requests_total,
    is_internal_client,
    s3_key_authentications_total,
    s3_multipart_uploads_in_flight,
    s3_signature_failures_total,
)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.9",
        "172.31.255.254",
        "192.168.1.10",
        "169.254.1.1",
        "::1",
        "fd00::1",
        "fe80::1",
    ],
)
def test_deployment_network_is_internal(host: str) -> None:
    assert is_internal_client(host)


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "1.1.1.1",
        "203.0.113.7",  # TEST-NET-3
        "192.0.2.1",  # TEST-NET-1
        "198.51.100.1",  # TEST-NET-2
        "198.18.0.1",  # benchmark range
        "172.32.0.1",  # just outside RFC1918
        "2001:4860:4860::8888",
        "example.com",
        "testclient",
        "",
    ],
)
def test_everything_else_is_external(host: str) -> None:
    assert not is_internal_client(host)


def test_missing_peer_is_external() -> None:
    assert not is_internal_client(None)


@pytest.mark.parametrize(
    "metric",
    [
        "cyberfs_http_requests_total",
        "cyberfs_http_request_duration_seconds",
        "cyberfs_bytes_uploaded_total",
        "cyberfs_bytes_downloaded_total",
        "cyberfs_crypto_operations_total",
        "cyberfs_crypto_operation_duration_seconds",
        "cyberfs_cache_operations_total",
        "cyberfs_cache_operation_duration_seconds",
        "cyberfs_quota_rejections_total",
        "cyberfs_job_runs_total",
        "cyberfs_job_duration_seconds",
    ],
)
def test_spec_named_instruments_exist(metric: str) -> None:
    """`deployment/spec.md`, "Observability", names each of these."""
    names = {m.name for m in REGISTRY.collect()}
    assert metric.removesuffix("_total") in names or metric in names


# --- protocol-labelled request metrics (task 10.1) -------------------------


def test_request_metrics_carry_a_protocol_label() -> None:
    """`s3-compatibility/spec.md`: request counts are labelled by protocol so S3
    and REST traffic can be told apart on the same series."""
    assert "protocol" in http_requests_total._labelnames
    assert "protocol" in http_request_duration_seconds._labelnames


@pytest.mark.parametrize("protocol", ["rest", "s3"])
def test_both_protocols_are_recordable(protocol: str) -> None:
    http_requests_total.labels(method="GET", route="/x", protocol=protocol, status="200").inc()
    http_request_duration_seconds.labels(method="GET", route="/x", protocol=protocol).observe(0.01)


class _FakeRequest:
    """Minimal stand-in exposing `scope['route']`, as Starlette does."""

    def __init__(self, tags: list[str] | None) -> None:
        route = type("Route", (), {"tags": tags, "path": "/whatever"})()
        self.scope = {"route": route} if tags is not None else {}


def test_protocol_label_marks_the_s3_router_and_nothing_else() -> None:
    """The S3 router tags its routes `s3`; every other route is `rest`. This is
    what lets a scrape tell the two protocols apart (`s3-compatibility/spec.md`)."""
    from cyberfs.adapters.inbound.api.metrics import protocol_label
    from cyberfs.adapters.inbound.api.routers.s3 import create_s3_router

    assert protocol_label(_FakeRequest(["s3"])) == "s3"
    assert protocol_label(_FakeRequest(["nodes"])) == "rest"
    assert protocol_label(_FakeRequest([])) == "rest"
    assert protocol_label(_FakeRequest(None)) == "rest"
    # And the real router genuinely carries the tag the label keys on.
    assert all("s3" in route.tags for route in create_s3_router("/s3").routes)


# --- S3-surface instruments (task 10.2) ------------------------------------


@pytest.mark.parametrize(
    "metric",
    [
        "cyberfs_s3_signature_failures_total",
        "cyberfs_s3_key_authentications_total",
        "cyberfs_s3_multipart_uploads_in_flight",
    ],
)
def test_s3_instruments_exist(metric: str) -> None:
    names = {m.name for m in REGISTRY.collect()}
    assert metric.removesuffix("_total") in names or metric in names


def test_s3_instruments_are_updatable() -> None:
    s3_signature_failures_total.labels(reason="signature_mismatch").inc()
    s3_key_authentications_total.labels(form="header").inc()
    s3_key_authentications_total.labels(form="presigned").inc()
    s3_multipart_uploads_in_flight.inc()
    s3_multipart_uploads_in_flight.dec()
