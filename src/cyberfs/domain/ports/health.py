"""Health probe port."""

from __future__ import annotations

from typing import Protocol

from cyberfs.domain.health import ComponentHealth, Criticality


class HealthProbe(Protocol):
    """A dependency that can report whether it is reachable."""

    @property
    def name(self) -> str: ...

    @property
    def criticality(self) -> Criticality: ...

    async def check(self) -> ComponentHealth: ...
