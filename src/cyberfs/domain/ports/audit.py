"""Audit sink port."""

from __future__ import annotations

from typing import Protocol

from cyberfs.domain.audit import AuditRecord


class AuditSink(Protocol):
    async def record(self, entry: AuditRecord) -> None: ...
