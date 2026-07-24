"""Non-blocking audit emission for file operations.

The one home of two cross-cutting rules `file-storage/spec.md` demands of every
operation site:

* a failed audit write is logged and swallowed, never surfaced -- losing a log
  line must not lose a user's file (task 1.5);
* a record whose actor does not own the node carries no file name, since a name
  is metadata the actor is not entitled to see about someone else's tree
  (task 1.6).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import NotFoundError, PermissionDeniedError
from cyberfs.domain.permissions import require_role
from cyberfs.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from cyberfs.domain.nodes import Node
    from cyberfs.domain.ports.repositories import UnitOfWork
    from cyberfs.domain.sharing import Role

logger = get_logger(__name__)


async def emit_audit(uow: UnitOfWork, record: AuditRecord) -> None:
    """Write an audit record without ever failing the caller's operation.

    A persistence error is logged at error level and dropped: the operation it
    describes has already succeeded, and the audit trail is evidence, not a
    precondition.
    """
    try:
        await uow.audit.add(record)
    except Exception:  # deliberately swallow every write failure
        logger.error(
            "audit_write_failed",
            action=str(record.action),
            target_id=record.target_id,
        )


async def emit_denial(
    uow: UnitOfWork,
    actor_subject: str,
    node_id: uuid.UUID,
    *,
    when: datetime | None = None,
) -> None:
    """Record an authorization denial for a refused file operation.

    A refused operation leaves an ``auth.denied`` record -- evidence that
    someone tried and was turned away -- carrying the actor and the node id
    only. The file name is never present: a caller denied access to a node is
    not entitled to its name (privacy invariant A). Non-blocking like every
    audit write, so recording the denial can never itself fail the request.
    """
    await emit_audit(
        uow,
        AuditRecord(
            action=AuditAction.AUTH_DENIED,
            occurred_at=when or utcnow(),
            actor_subject=actor_subject,
            target_id=str(node_id),
        ),
    )


async def authorize_or_record(
    uow: UnitOfWork,
    *,
    actor_subject: str,
    node_id: uuid.UUID,
    effective: Role | None,
    minimum: Role,
    when: datetime | None = None,
) -> Role:
    """Assert the caller holds `minimum`, recording an `auth.denied` on refusal.

    Wraps `require_role`: a `PermissionDeniedError` (403) or `NotFoundError`
    (404, "no access" masquerading as "not found") emits an authorization
    denial before propagating unchanged. The success path returns the role and
    writes nothing here, so a denied operation never yields a success record
    and an allowed one is never mistaken for a denial.
    """
    try:
        return require_role(effective, minimum, node_id=node_id)
    except (PermissionDeniedError, NotFoundError):
        await emit_denial(uow, actor_subject, node_id, when=when)
        raise


def owner_context(node: Node, actor_id: uuid.UUID) -> dict[str, Any]:
    """The file name, but only when the actor owns the node.

    Cross-user references stay id-only: the name is omitted for anyone but the
    owner, so an audit record never leaks a name out of the owner's tree.
    """
    if node.owner_id == actor_id:
        return {"name": node.name}
    return {}
