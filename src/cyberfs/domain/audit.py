"""Audit records.

Every authentication and authorization failure, every grant change, and every
privileged admin action is recorded. `sharing/spec.md` requires records to be
immutable through the API -- even for administrators -- so there is no update
or delete on this entity by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AuditAction(StrEnum):
    # --- authentication -----------------------------------------------
    AUTH_DENIED = "auth.denied"
    AUTH_RATE_LIMITED = "auth.rate_limited"
    USER_PROVISIONED = "user.provisioned"

    # --- sharing ------------------------------------------------------
    GRANT_CREATED = "grant.created"
    GRANT_UPDATED = "grant.updated"
    GRANT_REVOKED = "grant.revoked"
    OWNERSHIP_TRANSFERRED = "node.ownership_transferred"
    PUBLIC_LINK_CREATED = "public_link.created"
    PUBLIC_LINK_REVOKED = "public_link.revoked"
    PUBLIC_LINK_ACCESSED = "public_link.accessed"

    # --- encryption ---------------------------------------------------
    ENCRYPTION_ENABLED = "node.encryption_enabled"
    ENCRYPTION_DISABLED = "node.encryption_disabled"
    ENCRYPTION_OVERRIDDEN = "node.encryption_overridden"
    KEY_ROTATED = "key.rotated"

    # --- s3 access keys -----------------------------------------------
    # Security records: the lifecycle of a long-lived credential is retained,
    # never activity-pruned.
    S3_KEY_CREATED = "s3_key.created"
    S3_KEY_REVOKED = "s3_key.revoked"

    # --- administration -----------------------------------------------
    QUOTA_CHANGED = "admin.quota_changed"
    CACHE_PURGED = "admin.cache_purged"
    FILENAMES_REVEALED = "admin.filenames_revealed"
    BACKUP_TRIGGERED = "admin.backup_triggered"

    # --- file operations ----------------------------------------------
    FILE_UPLOADED = "file.uploaded"
    FILE_DOWNLOADED = "file.downloaded"
    NODE_CREATED = "node.created"
    NODE_RENAMED = "node.renamed"
    NODE_TAGS_CHANGED = "node.tags_changed"
    NODE_METADATA_CHANGED = "node.metadata_changed"
    NODE_MOVED = "node.moved"
    NODE_COPIED = "node.copied"
    NODE_DELETED = "node.deleted"
    NODE_RESTORED = "node.restored"
    VERSION_RESTORED = "version.restored"
    # Irreversible destruction. Deliberately absent from `ACTIVITY_ACTIONS`, so
    # it is classified as a security record and retained: a purge must stay
    # attributable long after the activity around it has been pruned.
    NODE_PURGED = "node.purged"


class AuditProtocol(StrEnum):
    """The surface an operation arrived through, so traffic can be attributed."""

    REST = "rest"
    S3 = "s3"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable fact about something that happened.

    `context` carries action-specific detail. It must never hold token values,
    key material, or file content; the logging redaction processor is the
    backstop, but callers are expected not to put them here in the first place.
    """

    action: AuditAction
    occurred_at: datetime
    protocol: AuditProtocol = AuditProtocol.REST
    actor_subject: str | None = None
    target_id: str | None = None
    recipient_subject: str | None = None
    reason: str | None = None
    source_ip: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
