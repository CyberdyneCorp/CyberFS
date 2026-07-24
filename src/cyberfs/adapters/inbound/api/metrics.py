"""Metrics endpoint and request instrumentation."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from cyberfs.infrastructure.metrics import (
    REGISTRY,
    http_request_duration_seconds,
    http_requests_total,
    is_internal_client,
)

router = APIRouter(tags=["metrics"])


def route_label(request: Request) -> str:
    """The templated path, so `/nodes/{node_id}` is one series, not millions."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._observe(request, HTTPStatus.INTERNAL_SERVER_ERROR, started)
            raise
        self._observe(request, response.status_code, started)
        return response

    @staticmethod
    def _observe(request: Request, status: int, started: float) -> None:
        route = route_label(request)
        http_requests_total.labels(method=request.method, route=route, status=str(status)).inc()
        http_request_duration_seconds.labels(method=request.method, route=route).observe(
            time.perf_counter() - started
        )


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    client = request.client.host if request.client else None
    if not is_internal_client(client):
        return PlainTextResponse("not found", status_code=int(HTTPStatus.NOT_FOUND))
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
