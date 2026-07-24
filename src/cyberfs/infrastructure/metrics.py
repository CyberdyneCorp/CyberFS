"""Prometheus metrics.

The instruments named in `deployment/spec.md`, "Observability": request counts
and latencies by route and status, bytes moved, encryption operations, cache
behaviour per dataset, quota rejections, and background job outcomes.
"""

from __future__ import annotations

import ipaddress

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

BYTE_BUCKETS = (
    1024.0,
    64 * 1024.0,
    1024**2,
    16 * 1024**2,
    128 * 1024**2,
    1024**3,
    float("inf"),
)

# --- requests --------------------------------------------------------------

http_requests_total = Counter(
    "cyberfs_http_requests_total",
    "HTTP requests by route and status.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "cyberfs_http_request_duration_seconds",
    "HTTP request latency by route.",
    labelnames=("method", "route"),
    registry=REGISTRY,
)

# --- content movement ------------------------------------------------------

bytes_uploaded_total = Counter(
    "cyberfs_bytes_uploaded_total",
    "Plaintext bytes accepted from clients.",
    labelnames=("encrypted",),
    registry=REGISTRY,
)

bytes_downloaded_total = Counter(
    "cyberfs_bytes_downloaded_total",
    "Plaintext bytes served to clients.",
    labelnames=("encrypted",),
    registry=REGISTRY,
)

# --- encryption ------------------------------------------------------------

crypto_operations_total = Counter(
    "cyberfs_crypto_operations_total",
    "Encryption operations by kind and outcome.",
    labelnames=("operation", "outcome"),
    registry=REGISTRY,
)

crypto_operation_duration_seconds = Histogram(
    "cyberfs_crypto_operation_duration_seconds",
    "Encryption operation latency by kind.",
    labelnames=("operation",),
    registry=REGISTRY,
)

# --- cache -----------------------------------------------------------------

cache_operations_total = Counter(
    "cyberfs_cache_operations_total",
    "Cache operations by dataset and outcome (hit, miss, error, timeout, eviction).",
    labelnames=("dataset", "outcome"),
    registry=REGISTRY,
)

cache_operation_duration_seconds = Histogram(
    "cyberfs_cache_operation_duration_seconds",
    "Cache operation latency by dataset.",
    labelnames=("dataset",),
    registry=REGISTRY,
)

# --- quota -----------------------------------------------------------------

quota_rejections_total = Counter(
    "cyberfs_quota_rejections_total",
    "Uploads refused because they would exceed the owner's quota.",
    registry=REGISTRY,
)

# --- background jobs -------------------------------------------------------

job_runs_total = Counter(
    "cyberfs_job_runs_total",
    "Background job runs by job and outcome.",
    labelnames=("job", "outcome"),
    registry=REGISTRY,
)

job_duration_seconds = Histogram(
    "cyberfs_job_duration_seconds",
    "Background job duration by job.",
    labelnames=("job",),
    registry=REGISTRY,
)


# Explicit ranges rather than `ip_address.is_private`, which also covers the
# documentation and benchmark blocks (192.0.2.0/24, 198.18.0.0/15,
# 203.0.113.0/24, …) -- "not globally routable" is not the same as "inside our
# deployment network", and treating it as such would expose /metrics.
_INTERNAL_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def is_internal_client(host: str | None) -> bool:
    """Whether a peer address is inside the deployment network.

    `deployment/spec.md` requires the metrics endpoint to refuse requests from
    outside. The check uses the direct peer, deliberately ignoring
    `X-Forwarded-For` -- a caller-supplied header cannot be an authorization
    signal. Anything that is not a recognisable internal address, including a
    hostname, is refused.
    """
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in _INTERNAL_NETWORKS)
