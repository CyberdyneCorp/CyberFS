"""Metrics access control and instrument surface."""

from __future__ import annotations

import pytest

from cyberfs.infrastructure.metrics import REGISTRY, is_internal_client


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
