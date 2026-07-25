"""Audit sink that writes to the structured log.

A durable, queryable sink backed by Postgres arrives with the data model; until
then audit records go to the log, which is already JSON, already correlated by
request id, and already redacted.
"""

from __future__ import annotations

from cyberfs.domain.audit import AuditRecord
from cyberfs.infrastructure.logging import get_logger

logger = get_logger("cyberfs.audit")


class LoggingAuditSink:
    """Implements the `AuditSink` port."""

    async def record(self, entry: AuditRecord) -> None:
        logger.info(
            "audit",
            action=str(entry.action),
            occurred_at=entry.occurred_at.isoformat(),
            actor_subject=entry.actor_subject,
            target_id=entry.target_id,
            recipient_subject=entry.recipient_subject,
            reason=entry.reason,
            source_ip=entry.source_ip,
            **entry.context,
        )
