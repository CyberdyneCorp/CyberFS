"""Readiness use case: run every probe, fold the results."""

from __future__ import annotations

import asyncio
import time

from cyberfs.domain.health import (
    ComponentHealth,
    ComponentStatus,
    ReadinessReport,
    evaluate_readiness,
)
from cyberfs.domain.ports.health import HealthProbe

DEFAULT_PROBE_TIMEOUT_SECONDS = 3.0


class HealthService:
    """Aggregates dependency probes into a readiness verdict.

    Probes are registered by the composition root as adapters come online, so
    this stays free of any knowledge of Postgres, Redis, or MinIO.
    """

    def __init__(
        self,
        probes: list[HealthProbe] | None = None,
        *,
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._probes: list[HealthProbe] = list(probes or [])
        self._timeout = timeout_seconds

    def register(self, probe: HealthProbe) -> None:
        self._probes.append(probe)

    async def readiness(self) -> ReadinessReport:
        results = await asyncio.gather(
            *(self._run(probe) for probe in self._probes),
            return_exceptions=False,
        )
        return evaluate_readiness(tuple(results))

    async def _run(self, probe: HealthProbe) -> ComponentHealth:
        """A probe that raises, hangs, or times out counts as down."""
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                return await probe.check()
        except TimeoutError:
            return self._down(probe, started, f"probe timed out after {self._timeout}s")
        except Exception as exc:
            return self._down(probe, started, type(exc).__name__)

    @staticmethod
    def _down(probe: HealthProbe, started: float, detail: str) -> ComponentHealth:
        return ComponentHealth(
            name=probe.name,
            status=ComponentStatus.DOWN,
            criticality=probe.criticality,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=detail,
        )
