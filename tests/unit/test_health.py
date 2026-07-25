"""Readiness folding and the health endpoints."""

from __future__ import annotations

import asyncio

import pytest

from cyberfs.application.health import HealthService
from cyberfs.domain.health import (
    ComponentHealth,
    ComponentStatus,
    Criticality,
    ReadinessStatus,
    evaluate_readiness,
)


def component(
    name: str,
    status: ComponentStatus,
    criticality: Criticality = Criticality.REQUIRED,
) -> ComponentHealth:
    return ComponentHealth(name=name, status=status, criticality=criticality)


class StubProbe:
    def __init__(
        self,
        name: str,
        status: ComponentStatus = ComponentStatus.UP,
        criticality: Criticality = Criticality.REQUIRED,
        *,
        raises: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._name = name
        self._status = status
        self._criticality = criticality
        self._raises = raises
        self._delay = delay

    @property
    def name(self) -> str:
        return self._name

    @property
    def criticality(self) -> Criticality:
        return self._criticality

    async def check(self) -> ComponentHealth:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return component(self._name, self._status, self._criticality)


# --- pure folding ----------------------------------------------------------


def test_all_up_is_ready() -> None:
    report = evaluate_readiness(
        (
            component("postgres", ComponentStatus.UP),
            component("minio", ComponentStatus.UP),
        )
    )
    assert report.status is ReadinessStatus.READY
    assert report.is_serving


def test_no_components_is_ready() -> None:
    assert evaluate_readiness(()).status is ReadinessStatus.READY


@pytest.mark.parametrize("failing", ["postgres", "minio"])
def test_required_component_down_is_not_ready(failing: str) -> None:
    components = tuple(
        component(name, ComponentStatus.DOWN if name == failing else ComponentStatus.UP)
        for name in ("postgres", "minio")
    )
    report = evaluate_readiness(components)
    assert report.status is ReadinessStatus.NOT_READY
    assert not report.is_serving


def test_optional_component_down_is_degraded_but_serving() -> None:
    """Redis down means slower, not broken -- `caching/spec.md`."""
    report = evaluate_readiness(
        (
            component("postgres", ComponentStatus.UP),
            component("cache", ComponentStatus.DOWN, Criticality.OPTIONAL),
        )
    )
    assert report.status is ReadinessStatus.DEGRADED
    assert report.is_serving


def test_required_failure_outranks_optional_failure() -> None:
    report = evaluate_readiness(
        (
            component("cache", ComponentStatus.DOWN, Criticality.OPTIONAL),
            component("postgres", ComponentStatus.DOWN),
        )
    )
    assert report.status is ReadinessStatus.NOT_READY


def test_disabled_component_is_not_a_fault() -> None:
    """A deliberately switched-off backup must not read as an outage."""
    report = evaluate_readiness(
        (
            component("postgres", ComponentStatus.UP),
            component("backup", ComponentStatus.DISABLED, Criticality.OPTIONAL),
        )
    )
    assert report.status is ReadinessStatus.READY


def test_failing_lists_only_down_components() -> None:
    report = evaluate_readiness(
        (
            component("postgres", ComponentStatus.UP),
            component("cache", ComponentStatus.DOWN, Criticality.OPTIONAL),
        )
    )
    assert [c.name for c in report.failing] == ["cache"]


# --- probe execution -------------------------------------------------------


async def test_service_runs_all_probes() -> None:
    service = HealthService([StubProbe("postgres"), StubProbe("minio")])
    report = await service.readiness()
    assert report.status is ReadinessStatus.READY
    assert {c.name for c in report.components} == {"postgres", "minio"}


async def test_raising_probe_counts_as_down() -> None:
    service = HealthService([StubProbe("postgres", raises=ConnectionError("refused"))])
    report = await service.readiness()
    assert report.status is ReadinessStatus.NOT_READY
    assert report.components[0].detail == "ConnectionError"


async def test_hanging_probe_times_out_rather_than_blocking_readiness() -> None:
    service = HealthService([StubProbe("minio", delay=5.0)], timeout_seconds=0.05)
    report = await service.readiness()
    assert report.status is ReadinessStatus.NOT_READY
    assert "timed out" in (report.components[0].detail or "")


async def test_optional_probe_failure_degrades_only() -> None:
    service = HealthService(
        [
            StubProbe("postgres"),
            StubProbe("cache", criticality=Criticality.OPTIONAL, raises=TimeoutError()),
        ]
    )
    report = await service.readiness()
    assert report.status is ReadinessStatus.DEGRADED


async def test_probes_can_be_registered_after_construction() -> None:
    service = HealthService()
    service.register(StubProbe("postgres", ComponentStatus.DOWN))
    report = await service.readiness()
    assert report.status is ReadinessStatus.NOT_READY


async def test_failed_probe_records_latency() -> None:
    service = HealthService([StubProbe("postgres", raises=ConnectionError())])
    report = await service.readiness()
    assert report.components[0].latency_ms is not None
