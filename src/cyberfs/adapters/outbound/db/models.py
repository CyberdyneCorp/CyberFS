"""SQLAlchemy 2.0 mapping.

Persistence shape only -- the invariants live on the domain entities in
`cyberfs.domain`, and these rows are translated to and from them by the
repositories. Indices are chosen for the four hot access paths: list a folder,
walk ancestors for permission resolution, resolve a subject's grants, and sweep
the trash.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TimestampTz = DateTime(timezone=True)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: CyberdyneAuth `sub`. The only identity CyberFS knows.
    subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    root_folder_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    org: Mapped[dict[str, object] | None] = mapped_column(postgresql.JSONB, nullable=True)
    orgs: Mapped[list[dict[str, object]] | None] = mapped_column(postgresql.JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)


class NodeRow(Base):
    """Adjacency list: one `parent_id`, no stored path."""

    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: NFC-folded; the uniqueness key, so two spellings of the same character
    #: cannot become two siblings that render identically.
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)

    # files
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # folders
    encryption_default: Mapped[str] = mapped_column(String(16), nullable=False, default="inherit")

    __table_args__ = (
        # Sibling names are unique among *live* rows only: deleting a file must
        # free its name for reuse, which a plain unique constraint would not
        # allow while the soft-deleted row lingers in the trash.
        Index(
            "uq_nodes_parent_name_live",
            "parent_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Folder listings.
        Index(
            "ix_nodes_parent_live",
            "parent_id",
            "normalized_name",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Owner-scoped lookups and quota reconciliation.
        Index("ix_nodes_owner", "owner_id"),
        # The trash sweep: find everything deleted before a cutoff. Deliberately
        # NOT owner-scoped -- the sweep reads across every user, so narrowing it
        # to one would defeat it.
        Index(
            "ix_nodes_deleted_at",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
        # One owner's trash listing, which the sweep's index cannot serve. Narrows
        # to the caller's trashed rows in deletion order first; the "parent is not
        # trashed" test is then a primary-key lookup per candidate.
        Index(
            "ix_nodes_owner_trash",
            "owner_id",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
        # Metadata search.
        Index("ix_nodes_owner_name", "owner_id", "normalized_name"),
    )


class FileVersionRow(Base):
    __tablename__ = "file_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: SHA-256 of the plaintext, so integrity is checkable after decryption.
    plaintext_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint("node_id", "sequence", name="uq_file_versions_node_sequence"),
        Index("ix_file_versions_node", "node_id", "sequence"),
    )


class NodeTagRow(Base):
    """One label on one node.

    A row per tag rather than an array or JSONB column: the limits are then a
    plain count, and a filtered search is an ordinary join on an indexed column
    instead of a containment operator.
    """

    __tablename__ = "node_tags"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    #: Already normalized by the domain; stored exactly as it is matched.
    tag: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("node_id", "tag", name="uq_node_tags_node_tag"),
        # Searching by tag looks up the tag first, then narrows to nodes.
        Index("ix_node_tags_tag", "tag"),
    )


class NodeMetadataRow(Base):
    """One key/value pair on one node."""

    __tablename__ = "node_metadata"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(String(1024), nullable=False)

    __table_args__ = (
        UniqueConstraint("node_id", "key", name="uq_node_metadata_node_key"),
        # Serves both supported queries: key alone, and key with value.
        Index("ix_node_metadata_key_value", "key", "value"),
    )


class GrantRow(Base):
    __tablename__ = "grants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Stored as the ordinal so "highest role" is a `MAX()` in SQL.
    role: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    #: A grant whose large-subtree rewrap has been handed to the background
    #: worker and has not yet completed. A pending grant confers no access and
    #: is invisible to the recipient. Defaults false so every existing grant --
    #: synchronously rewrapped -- stays active.
    pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    __table_args__ = (
        # A regrant replaces the role rather than adding a second row.
        UniqueConstraint("node_id", "subject", name="uq_grants_node_subject"),
        # Effective-permission resolution: given an ancestor chain and a
        # subject, take the highest role.
        Index("ix_grants_subject_node", "subject", "node_id"),
        Index("ix_grants_node", "node_id"),
        # The rewrap worker's driver: find pending grants cheaply.
        Index(
            "ix_grants_pending",
            "created_at",
            postgresql_where=text("pending"),
        ),
    )


class PublicLinkRow(Base):
    __tablename__ = "public_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    #: Only the hash: a database leak must not yield working links.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    passphrase_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)

    __table_args__ = (
        Index("ix_public_links_node", "node_id"),
        Index(
            "ix_public_links_active",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class UserKeyRow(Base):
    """A user's KEK, sealed under `MASTER_KEY`. Never stored in the clear."""

    __tablename__ = "user_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    wrapped_kek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Which master key sealed it; drives resumable rotation.
    master_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)

    __table_args__ = (Index("ix_user_keys_master", "master_key_id"),)


class WrappedDataKeyRow(Base):
    """One file's DEK sealed under one subject's KEK.

    A row exists for exactly the principals who may read the file. Revoking a
    grant deletes the corresponding row in the same transaction.
    """

    __tablename__ = "wrapped_data_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)

    __table_args__ = (
        UniqueConstraint("node_id", "subject", name="uq_wrapped_data_keys_node_subject"),
        Index("ix_wrapped_data_keys_node", "node_id"),
    )


class S3AccessKeyRow(Base):
    """An S3 access-key credential. The secret is stored only sealed under
    `MASTER_KEY` -- never in the clear, and never recoverable from this row
    alone."""

    __tablename__ = "s3_access_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: The public access-key id, unique so a signed request resolves to exactly
    #: one credential.
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The secret access key, sealed under `MASTER_KEY`. A database leak alone
    #: yields nothing without the master key held outside the database.
    sealed_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Which master key sealed the secret; drives resumable rotation.
    secret_master_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    #: A key must not outlive its account, so it cascades with the user row.
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The CyberdyneAuth subject a request signed with this key resolves to.
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)

    __table_args__ = (
        # The signing hot path is a single lookup by key id; it must be unique.
        Index("uq_s3_access_keys_key_id", "key_id", unique=True),
        # Listing and rotation read by owner.
        Index("ix_s3_access_keys_owner", "owner_subject"),
    )


class MultipartUploadRow(Base):
    """One in-flight multipart upload.

    The write target is stored decomposed so completion rebuilds the exact
    address the parts assemble into, without re-parsing a raw key. `created_at`
    is indexed for the reaper's abandonment sweep.
    """

    __tablename__ = "multipart_uploads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: The opaque upload id an S3 client echoes on every part and on completion.
    upload_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The subject that created the upload; only they may add to or finish it.
    initiator_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Whose tree the assembled object lands in -- the caller's, or another
    #: owner's under the shared prefix.
    target_owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The node path the object assembles to, matching `NodePath.path`.
    target_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    via_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)

    __table_args__ = (
        # The hot path is a single lookup by upload id; parts reference it.
        Index("uq_multipart_uploads_upload_id", "upload_id", unique=True),
        # The abandonment sweep reads by age.
        Index("ix_multipart_uploads_created_at", "created_at"),
    )


class MultipartPartRow(Base):
    """One staged part of a multipart upload.

    The bytes live at `object_key` under the reserved staging prefix; this row
    records the part number, its S3 ETag, and its size so completion can
    validate the client's part list and charge the summed size once.
    """

    __tablename__ = "multipart_parts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    upload_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("multipart_uploads.upload_id", ondelete="CASCADE"),
        nullable=False,
    )
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)
    etag: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)

    __table_args__ = (
        # A part number is unique within an upload; a re-upload replaces it.
        UniqueConstraint("upload_id", "part_number", name="uq_multipart_parts_upload_number"),
        Index("ix_multipart_parts_upload", "upload_id"),
    )


class QuotaUsageRow(Base):
    __tablename__ = "quota_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    live_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    trashed_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)


class BackupRecordRow(Base):
    """Durable history of one backup run.

    Lives in its own table, written outside any request transaction: a backup
    runs on the scheduler's own session, not a Unit of Work. Failures are kept
    alongside successes so history can show a run that never succeeded.
    """

    __tablename__ = "backup_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    started_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampTz, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    dump_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    skew_missing_in_dump: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skew_missing_in_manifest: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Staleness ("latest verified") and history-by-time both read by
        # state and finish time.
        Index("ix_backup_records_state_finished", "state", "finished_at"),
        Index("ix_backup_records_started", "started_at"),
    )


class AuditRow(Base):
    """Append-only. There is no update or delete path, by design."""

    __tablename__ = "audit_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TimestampTz, nullable=False)
    #: The surface the operation arrived through -- `rest` or `s3` -- so REST
    #: and S3 traffic can be told apart. Defaults to `rest` for existing rows.
    protocol: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="rest", default="rest"
    )
    actor_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recipient_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context: Mapped[dict[str, object] | None] = mapped_column(postgresql.JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_audit_occurred_at", "occurred_at"),
        Index("ix_audit_actor", "actor_subject", "occurred_at"),
        Index("ix_audit_action", "action", "occurred_at"),
        Index("ix_audit_target", "target_id", "occurred_at"),
    )
