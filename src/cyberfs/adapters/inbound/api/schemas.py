"""Request and response models.

The wire contract. Domain entities are never serialized directly, so a field
added to an entity cannot leak into an API response by accident.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cyberfs.application.nodes import NodeView
from cyberfs.domain.audit import AuditRecord
from cyberfs.domain.backup import BackupRecord, is_stale
from cyberfs.domain.nodes import EncryptionDefault, FileVersion, Node, NodeKind
from cyberfs.domain.ports.repositories import Page
from cyberfs.domain.sharing import Grant, PublicLink
from cyberfs.domain.stats import JobStatus, TenantStatistics, UserStorage

MAX_NAME_LENGTH = 255


class CreateFolderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    encryption_default: EncryptionDefault = EncryptionDefault.INHERIT


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: uuid.UUID


class CopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)


class NodeSummary(BaseModel):
    """A node in a listing. No content, and no key material anywhere."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: NodeKind
    name: str
    size_bytes: int
    content_type: str | None
    encrypted: bool
    encryption_default: EncryptionDefault | None
    created_at: datetime
    updated_at: datetime
    etag: str

    @classmethod
    def of(cls, node: Node) -> NodeSummary:
        return cls(
            id=node.id,
            parent_id=node.parent_id,
            kind=node.kind,
            name=node.name,
            size_bytes=node.size_bytes,
            content_type=node.content_type,
            encrypted=node.encrypted,
            # Meaningless on a file; omitted rather than reported as "inherit".
            encryption_default=node.encryption_default if node.is_folder else None,
            created_at=node.created_at,
            updated_at=node.updated_at,
            etag=node.etag,
        )


class NodeDetail(NodeSummary):
    """A single node, with the caller's own permission and its derived path."""

    path: str
    #: The caller's effective role: `viewer`, `editor`, or `owner`.
    role: str

    @classmethod
    def of_view(cls, view: NodeView) -> NodeDetail:
        summary = NodeSummary.of(view.node)
        return cls(**summary.model_dump(), path=view.path, role=view.role.slug)


class NodePage(BaseModel):
    items: list[NodeSummary]
    next_cursor: str | None = None

    @classmethod
    def of(cls, page: Page[Node]) -> NodePage:
        return cls(
            items=[NodeSummary.of(node) for node in page.items],
            next_cursor=page.next_cursor,
        )


class SearchResults(BaseModel):
    items: list[NodeSummary]

    @classmethod
    def of(cls, nodes: tuple[Node, ...]) -> SearchResults:
        return cls(items=[NodeSummary.of(node) for node in nodes])


class DeleteResult(BaseModel):
    #: How many nodes the recursive soft delete covered.
    deleted: int


class VersionSummary(BaseModel):
    """One retained revision. Carries no key material and no object key."""

    id: uuid.UUID
    sequence: int
    size_bytes: int
    content_type: str
    encrypted: bool
    created_at: datetime
    created_by: str
    is_current: bool = False

    @classmethod
    def of(cls, version: FileVersion, *, current: bool = False) -> VersionSummary:
        return cls(
            id=version.id,
            sequence=version.sequence,
            size_bytes=version.size_bytes,
            content_type=version.content_type,
            encrypted=version.encrypted,
            created_at=version.created_at,
            created_by=version.created_by,
            is_current=current,
        )


class VersionList(BaseModel):
    items: list[VersionSummary]

    @classmethod
    def of(cls, versions: tuple[FileVersion, ...]) -> VersionList:
        newest = versions[0].id if versions else None
        return cls(items=[VersionSummary.of(v, current=v.id == newest) for v in versions])


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A CyberdyneAuth subject, or an email resolvable within the sharer's orgs.
    recipient: str = Field(min_length=1, max_length=320)
    role: Literal["viewer", "editor", "owner"]


class GrantSummary(BaseModel):
    id: uuid.UUID
    node_id: uuid.UUID
    subject: str
    role: str
    granted_by: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, grant: Grant) -> GrantSummary:
        return cls(
            id=grant.id,
            node_id=grant.node_id,
            subject=grant.subject,
            role=grant.role.slug,
            granted_by=grant.granted_by,
            created_at=grant.created_at,
            updated_at=grant.updated_at,
        )


class GrantList(BaseModel):
    items: list[GrantSummary]

    @classmethod
    def of(cls, grants: tuple[Grant, ...]) -> GrantList:
        return cls(items=[GrantSummary.of(g) for g in grants])


class CreateLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_at: datetime | None = None
    passphrase: str | None = Field(default=None, min_length=4, max_length=255)


class LinkSummary(BaseModel):
    """A link as its owner sees it. The token is never echoed back."""

    id: uuid.UUID
    node_id: uuid.UUID
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    revoked: bool
    passphrase_protected: bool
    access_count: int
    last_accessed_at: datetime | None

    @classmethod
    def of(cls, link: PublicLink) -> LinkSummary:
        return cls(
            id=link.id,
            node_id=link.node_id,
            created_by=link.created_by,
            created_at=link.created_at,
            expires_at=link.expires_at,
            revoked=link.is_revoked,
            passphrase_protected=link.requires_passphrase,
            access_count=link.access_count,
            last_accessed_at=link.last_accessed_at,
        )


class IssuedLinkResponse(LinkSummary):
    """Returned once, at creation. The token is not recoverable afterwards."""

    token: str

    @classmethod
    def of_issued(cls, link: PublicLink, token: str) -> IssuedLinkResponse:
        return cls(**LinkSummary.of(link).model_dump(), token=token)


class LinkList(BaseModel):
    items: list[LinkSummary]

    @classmethod
    def of(cls, links: tuple[PublicLink, ...]) -> LinkList:
        return cls(items=[LinkSummary.of(link) for link in links])


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str = Field(min_length=1, max_length=320)
    #: Leave the previous owner an explicit editor grant, per the spec default.
    keep_editor_access: bool = True


class EncryptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Turning this off lowers protection; it requires `owner` and is audited.
    encrypted: bool


class UserStorageSummary(BaseModel):
    """One user's consumption. Metadata only -- no names, no content."""

    user_id: uuid.UUID
    subject: str
    quota_bytes: int
    used_bytes: int
    live_bytes: int
    trashed_bytes: int
    version_bytes: int
    percent_used: float
    over_quota: bool
    file_count: int
    folder_count: int
    encrypted_file_count: int
    encrypted_bytes: int
    encrypted_share: float
    grants_given: int
    grants_received: int
    is_admin: bool
    created_at: datetime | None
    last_seen_at: datetime | None

    @classmethod
    def of(cls, stats: UserStorage) -> UserStorageSummary:
        return cls(
            user_id=stats.user_id,
            subject=stats.subject,
            quota_bytes=stats.quota_bytes,
            used_bytes=stats.used_bytes,
            live_bytes=stats.live_bytes,
            trashed_bytes=stats.trashed_bytes,
            version_bytes=stats.version_bytes,
            percent_used=stats.percent_used,
            over_quota=stats.over_quota,
            file_count=stats.file_count,
            folder_count=stats.folder_count,
            encrypted_file_count=stats.encrypted_file_count,
            encrypted_bytes=stats.encrypted_bytes,
            encrypted_share=stats.encrypted_share,
            grants_given=stats.grants_given,
            grants_received=stats.grants_received,
            is_admin=stats.is_admin,
            created_at=stats.created_at,
            last_seen_at=stats.last_seen_at,
        )


class UserStorageList(BaseModel):
    items: list[UserStorageSummary]

    @classmethod
    def of(cls, users: tuple[UserStorage, ...]) -> UserStorageList:
        return cls(items=[UserStorageSummary.of(u) for u in users])


class ContentTypeSlice(BaseModel):
    content_type: str
    file_count: int
    bytes: int


class GrowthSlice(BaseModel):
    day: date
    files_added: int
    bytes_added: int


class TenantSummary(BaseModel):
    total_bytes: int
    live_bytes: int
    trashed_bytes: int
    version_bytes: int
    file_count: int
    folder_count: int
    user_count: int
    active_user_count: int
    encrypted_file_count: int
    encrypted_bytes: int
    encrypted_share: float
    public_link_count: int
    grant_count: int
    content_types: list[ContentTypeSlice]
    growth: list[GrowthSlice]
    top_consumers: list[UserStorageSummary]

    @classmethod
    def of(cls, stats: TenantStatistics) -> TenantSummary:
        return cls(
            total_bytes=stats.total_bytes,
            live_bytes=stats.live_bytes,
            trashed_bytes=stats.trashed_bytes,
            version_bytes=stats.version_bytes,
            file_count=stats.file_count,
            folder_count=stats.folder_count,
            user_count=stats.user_count,
            active_user_count=stats.active_user_count,
            encrypted_file_count=stats.encrypted_file_count,
            encrypted_bytes=stats.encrypted_bytes,
            encrypted_share=stats.encrypted_share,
            public_link_count=stats.public_link_count,
            grant_count=stats.grant_count,
            content_types=[
                ContentTypeSlice(
                    content_type=c.content_type, file_count=c.file_count, bytes=c.bytes
                )
                for c in stats.content_types
            ],
            growth=[
                GrowthSlice(day=g.day, files_added=g.files_added, bytes_added=g.bytes_added)
                for g in stats.growth
            ],
            top_consumers=[UserStorageSummary.of(u) for u in stats.top_consumers],
        )


class QuotaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quota_bytes: int = Field(ge=0)


class AuditEntry(BaseModel):
    action: str
    occurred_at: datetime
    actor_subject: str | None
    target_id: str | None
    recipient_subject: str | None
    reason: str | None
    source_ip: str | None
    context: dict[str, Any]

    @classmethod
    def of(cls, record: AuditRecord) -> AuditEntry:
        return cls(
            action=str(record.action),
            occurred_at=record.occurred_at,
            actor_subject=record.actor_subject,
            target_id=record.target_id,
            recipient_subject=record.recipient_subject,
            reason=record.reason,
            source_ip=record.source_ip,
            context=dict(record.context),
        )


class AuditPage(BaseModel):
    items: list[AuditEntry]
    next_cursor: str | None = None

    @classmethod
    def of(cls, page: Page[AuditRecord]) -> AuditPage:
        return cls(items=[AuditEntry.of(r) for r in page.items], next_cursor=page.next_cursor)


class JobSummary(BaseModel):
    name: str
    last_run_at: datetime | None
    outcome: str | None
    duration_seconds: float | None
    detail: str | None
    has_run: bool

    @classmethod
    def of(cls, status: JobStatus) -> JobSummary:
        return cls(
            name=status.name,
            last_run_at=status.last_run_at,
            outcome=status.outcome,
            duration_seconds=status.duration_seconds,
            detail=status.detail,
            has_run=status.has_run,
        )


class BackupRecordSummary(BaseModel):
    """One backup run for the operations listing.

    Metadata about the run only -- checksums and counts, never key material or
    an object key. A backup identified by timestamp, verification state, size,
    and schema revision, as `backup-restore/spec.md` requires for point-in-time
    selection.
    """

    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    state: str
    verified: bool
    dump_checksum: str | None
    object_count: int
    total_bytes: int
    schema_revision: str
    duration_seconds: float | None
    skew_missing_in_dump: int
    skew_missing_in_manifest: int
    has_skew: bool
    error: str | None

    @classmethod
    def of(cls, record: BackupRecord) -> BackupRecordSummary:
        return cls(
            id=record.id,
            started_at=record.started_at,
            finished_at=record.finished_at,
            state=str(record.state),
            verified=record.is_verified,
            dump_checksum=record.dump_checksum,
            object_count=record.object_count,
            total_bytes=record.total_bytes,
            schema_revision=record.schema_revision,
            duration_seconds=record.duration_seconds,
            skew_missing_in_dump=record.skew_missing_in_dump,
            skew_missing_in_manifest=record.skew_missing_in_manifest,
            has_skew=record.has_skew,
            error=record.error,
        )


class BackupList(BaseModel):
    items: list[BackupRecordSummary]

    @classmethod
    def of(cls, records: tuple[BackupRecord, ...]) -> BackupList:
        return cls(items=[BackupRecordSummary.of(r) for r in records])


class BackupSummary(BaseModel):
    """The backup subsystem's state for the operations view.

    Surfaces the last run's time, outcome, duration, size, and verification,
    plus a staleness alert flag raised when no verified backup has completed
    within `BACKUP_MAX_AGE_HOURS` (`backup-restore/spec.md`, "Backup
    observability").
    """

    enabled: bool
    stale: bool
    last_backup_at: datetime | None = None
    last_outcome: str | None = None
    last_duration_seconds: float | None = None
    last_size_bytes: int | None = None
    last_verified: bool | None = None
    last_verified_at: datetime | None = None
    object_count: int | None = None
    schema_revision: str | None = None

    @classmethod
    def of(
        cls,
        records: tuple[BackupRecord, ...],
        *,
        enabled: bool,
        max_age_hours: int,
        now: datetime,
    ) -> BackupSummary:
        latest = records[0] if records else None
        latest_verified = next((r for r in records if r.is_verified), None)
        last_verified_at = latest_verified.finished_at if latest_verified else None
        stale = enabled and is_stale(last_verified_at, max_age_hours=max_age_hours, now=now)
        if latest is None:
            return cls(enabled=enabled, stale=stale)
        return cls(
            enabled=enabled,
            stale=stale,
            last_backup_at=latest.finished_at or latest.started_at,
            last_outcome=str(latest.state),
            last_duration_seconds=latest.duration_seconds,
            last_size_bytes=latest.total_bytes,
            last_verified=latest.is_verified,
            last_verified_at=last_verified_at,
            object_count=latest.object_count,
            schema_revision=latest.schema_revision,
        )


class OperationsSummary(BaseModel):
    """Dependency and job state. Reports counts, never cached values."""

    components: list[dict[str, Any]]
    jobs: list[JobSummary]
    cache: dict[str, Any]
    totals_reconcile: bool
    backup: BackupSummary


class PurgeResponse(BaseModel):
    dataset: str
    keys_removed: int
