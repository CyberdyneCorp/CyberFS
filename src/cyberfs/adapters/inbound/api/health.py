"""Liveness and readiness endpoints.

Liveness must not consult dependencies: a Postgres outage should not make the
orchestrator restart-loop an otherwise healthy container.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cyberfs import __version__
from cyberfs.application.health import HealthService
from cyberfs.domain.health import ReadinessReport, ReadinessStatus

router = APIRouter(tags=["health"])


def readiness_components(report: ReadinessReport) -> list[dict[str, Any]]:
    """The per-component view, shared with the admin operations page."""
    return [
        {
            "name": component.name,
            "status": str(component.status),
            "criticality": str(component.criticality),
            "latency_ms": (
                round(component.latency_ms, 2) if component.latency_ms is not None else None
            ),
            "detail": component.detail,
        }
        for component in report.components
    ]


def serialize_readiness(report: ReadinessReport) -> dict[str, Any]:
    return {
        "status": str(report.status),
        "version": __version__,
        "components": readiness_components(report),
    }


@router.get("/health/live", summary="Process liveness")
async def live() -> dict[str, str]:
    return {"status": "alive", "version": __version__}


@router.get("/health/ready", summary="Dependency readiness")
async def ready(request: Request) -> JSONResponse:
    service: HealthService = request.app.state.health
    report = await service.readiness()
    status = (
        HTTPStatus.SERVICE_UNAVAILABLE
        if report.status is ReadinessStatus.NOT_READY
        else HTTPStatus.OK
    )
    return JSONResponse(status_code=int(status), content=serialize_readiness(report))
