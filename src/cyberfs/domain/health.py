"""Health aggregation.

Liveness reflects only process health; readiness reflects dependencies, and
distinguishes *degraded but serving* from *not serving* -- see
`deployment/spec.md`, "Health and readiness". Redis being down is degraded:
CyberFS stays correct without it, just slower (`caching/spec.md`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ComponentStatus(StrEnum):
    UP = "up"
    DOWN = "down"
    DISABLED = "disabled"


class Criticality(StrEnum):
    #: Down means the replica cannot serve; readiness fails.
    REQUIRED = "required"
    #: Down means slower or reduced function; readiness stays healthy.
    OPTIONAL = "optional"


class ReadinessStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    status: ComponentStatus
    criticality: Criticality
    latency_ms: float | None = None
    detail: str | None = None

    @property
    def is_failing(self) -> bool:
        return self.status is ComponentStatus.DOWN


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: ReadinessStatus
    components: tuple[ComponentHealth, ...] = field(default=())

    @property
    def is_serving(self) -> bool:
        """Whether this replica should stay in rotation."""
        return self.status is not ReadinessStatus.NOT_READY

    @property
    def failing(self) -> tuple[ComponentHealth, ...]:
        return tuple(c for c in self.components if c.is_failing)


def evaluate_readiness(components: tuple[ComponentHealth, ...]) -> ReadinessReport:
    """Fold component health into an overall readiness verdict.

    A failing REQUIRED component means not ready. A failing OPTIONAL component
    means degraded -- still serving. Disabled components are ignored: a
    deliberately switched-off backup is not a fault.
    """
    status = ReadinessStatus.READY
    for component in components:
        if not component.is_failing:
            continue
        if component.criticality is Criticality.REQUIRED:
            return ReadinessReport(ReadinessStatus.NOT_READY, components)
        status = ReadinessStatus.DEGRADED
    return ReadinessReport(status, components)
