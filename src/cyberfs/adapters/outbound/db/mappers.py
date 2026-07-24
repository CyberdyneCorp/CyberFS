"""Translate between persistence rows and domain entities.

Kept separate so the domain never imports SQLAlchemy and the rows never carry
behaviour.
"""

from __future__ import annotations

from typing import Any

from cyberfs.adapters.outbound.db import models as m
from cyberfs.domain.audit import AuditAction, AuditProtocol, AuditRecord
from cyberfs.domain.auth.principal import Org
from cyberfs.domain.backup import BackupRecord, BackupState
from cyberfs.domain.keys import UserKey, WrappedDataKey
from cyberfs.domain.nodes import EncryptionDefault, FileVersion, Node, NodeKind
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.multipart import MultipartPart, MultipartUpload
from cyberfs.domain.sharing import Grant, PublicLink, Role
from cyberfs.domain.users import QuotaUsage, User


def _org_json(org: Org) -> dict[str, Any]:
    return {"id": org.id, "short_name": org.short_name, "github_login": org.github_login}


def _org_to_json(org: Org | None) -> dict[str, Any] | None:
    return None if org is None else _org_json(org)


def _orgs_to_json(orgs: tuple[Org, ...]) -> list[dict[str, Any]]:
    return [_org_json(org) for org in orgs]


def _org_from_json(raw: Any) -> Org | None:
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    return Org(
        id=str(raw["id"]),
        short_name=str(raw.get("short_name") or ""),
        github_login=raw.get("github_login"),
    )


def _orgs_from_json(raw: Any) -> tuple[Org, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(org for entry in raw if (org := _org_from_json(entry)) is not None)


# --- users -----------------------------------------------------------------


def user_to_row(user: User) -> m.UserRow:
    return m.UserRow(
        id=user.id,
        subject=user.subject,
        root_folder_id=user.root_folder_id,
        quota_bytes=user.quota_bytes,
        is_admin=user.is_admin,
        org=_org_to_json(user.org),
        orgs=_orgs_to_json(user.orgs),
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_seen_at=user.last_seen_at,
    )


def apply_user(row: m.UserRow, user: User) -> None:
    row.quota_bytes = user.quota_bytes
    row.is_admin = user.is_admin
    row.org = _org_to_json(user.org)
    row.orgs = _orgs_to_json(user.orgs)
    row.updated_at = user.updated_at
    row.last_seen_at = user.last_seen_at


def user_from_row(row: m.UserRow) -> User:
    return User(
        id=row.id,
        subject=row.subject,
        root_folder_id=row.root_folder_id,
        quota_bytes=row.quota_bytes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_admin=row.is_admin,
        org=_org_from_json(row.org),
        orgs=_orgs_from_json(row.orgs),
        last_seen_at=row.last_seen_at,
    )


# --- nodes -----------------------------------------------------------------


def node_to_row(node: Node) -> m.NodeRow:
    return m.NodeRow(
        id=node.id,
        owner_id=node.owner_id,
        parent_id=node.parent_id,
        kind=str(node.kind),
        name=node.name,
        normalized_name=node.normalized_name,
        revision=node.revision,
        created_at=node.created_at,
        updated_at=node.updated_at,
        deleted_at=node.deleted_at,
        current_version_id=node.current_version_id,
        size_bytes=node.size_bytes,
        content_type=node.content_type,
        encrypted=node.encrypted,
        encryption_default=str(node.encryption_default),
    )


def apply_node(row: m.NodeRow, node: Node) -> None:
    # Ownership is mutable: transferring a subtree rewrites it.
    row.owner_id = node.owner_id
    row.parent_id = node.parent_id
    row.name = node.name
    row.normalized_name = node.normalized_name
    row.revision = node.revision
    row.updated_at = node.updated_at
    row.deleted_at = node.deleted_at
    row.current_version_id = node.current_version_id
    row.size_bytes = node.size_bytes
    row.content_type = node.content_type
    row.encrypted = node.encrypted
    row.encryption_default = str(node.encryption_default)


def node_from_row(row: m.NodeRow) -> Node:
    return Node(
        id=row.id,
        owner_id=row.owner_id,
        kind=NodeKind(row.kind),
        name=row.name,
        parent_id=row.parent_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        revision=row.revision,
        deleted_at=row.deleted_at,
        current_version_id=row.current_version_id,
        size_bytes=row.size_bytes,
        content_type=row.content_type,
        encrypted=row.encrypted,
        encryption_default=EncryptionDefault(row.encryption_default),
    )


def node_from_mapping(data: dict[str, Any]) -> Node:
    """Build a node from a raw CTE result row."""
    return Node(
        id=data["id"],
        owner_id=data["owner_id"],
        kind=NodeKind(data["kind"]),
        name=data["name"],
        parent_id=data["parent_id"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        revision=data["revision"],
        deleted_at=data["deleted_at"],
        current_version_id=data["current_version_id"],
        size_bytes=data["size_bytes"],
        content_type=data["content_type"],
        encrypted=data["encrypted"],
        encryption_default=EncryptionDefault(data["encryption_default"]),
    )


# --- versions --------------------------------------------------------------


def version_to_row(version: FileVersion) -> m.FileVersionRow:
    return m.FileVersionRow(
        id=version.id,
        node_id=version.node_id,
        owner_id=version.owner_id,
        sequence=version.sequence,
        size_bytes=version.size_bytes,
        plaintext_digest=version.plaintext_digest,
        content_type=version.content_type,
        encrypted=version.encrypted,
        created_at=version.created_at,
        created_by=version.created_by,
    )


def version_from_row(row: m.FileVersionRow) -> FileVersion:
    return FileVersion(
        id=row.id,
        node_id=row.node_id,
        owner_id=row.owner_id,
        sequence=row.sequence,
        size_bytes=row.size_bytes,
        plaintext_digest=row.plaintext_digest,
        content_type=row.content_type,
        encrypted=row.encrypted,
        created_at=row.created_at,
        created_by=row.created_by,
    )


# --- keys ------------------------------------------------------------------


def user_key_to_row(key: UserKey) -> m.UserKeyRow:
    return m.UserKeyRow(
        user_id=key.user_id,
        wrapped_kek=key.wrapped_kek,
        master_key_id=key.master_key_id,
        created_at=key.created_at,
        rotated_at=key.rotated_at,
    )


def user_key_from_row(row: m.UserKeyRow) -> UserKey:
    return UserKey(
        user_id=row.user_id,
        wrapped_kek=row.wrapped_kek,
        master_key_id=row.master_key_id,
        created_at=row.created_at,
        rotated_at=row.rotated_at,
    )


def data_key_to_row(key: WrappedDataKey) -> m.WrappedDataKeyRow:
    return m.WrappedDataKeyRow(
        id=key.id,
        node_id=key.node_id,
        subject=key.subject,
        wrapped_dek=key.wrapped_dek,
        created_at=key.created_at,
    )


def data_key_from_row(row: m.WrappedDataKeyRow) -> WrappedDataKey:
    return WrappedDataKey(
        id=row.id,
        node_id=row.node_id,
        subject=row.subject,
        wrapped_dek=row.wrapped_dek,
        created_at=row.created_at,
    )


# --- s3 access keys --------------------------------------------------------


def s3_access_key_to_row(key: S3AccessKey) -> m.S3AccessKeyRow:
    return m.S3AccessKeyRow(
        key_id=key.key_id,
        sealed_secret=key.sealed_secret,
        secret_master_key_id=key.secret_master_key_id,
        label=key.label,
        user_id=key.owner_id,
        owner_subject=key.owner_subject,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


def apply_s3_access_key(row: m.S3AccessKeyRow, key: S3AccessKey) -> None:
    # Only the mutable lifecycle timestamps change after creation.
    row.last_used_at = key.last_used_at
    row.revoked_at = key.revoked_at


def s3_access_key_from_row(row: m.S3AccessKeyRow) -> S3AccessKey:
    return S3AccessKey(
        key_id=row.key_id,
        sealed_secret=row.sealed_secret,
        secret_master_key_id=row.secret_master_key_id,
        label=row.label,
        owner_id=row.user_id,
        owner_subject=row.owner_subject,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


# --- multipart uploads -----------------------------------------------------


def multipart_upload_to_row(upload: MultipartUpload) -> m.MultipartUploadRow:
    return m.MultipartUploadRow(
        upload_id=upload.upload_id,
        initiator_subject=upload.initiator_subject,
        target_owner_subject=upload.target_owner_subject,
        target_key=upload.target_key,
        via_shared=upload.via_shared,
        content_type=upload.content_type,
        created_at=upload.created_at,
    )


def multipart_upload_from_row(row: m.MultipartUploadRow) -> MultipartUpload:
    return MultipartUpload(
        upload_id=row.upload_id,
        initiator_subject=row.initiator_subject,
        target_owner_subject=row.target_owner_subject,
        target_key=row.target_key,
        via_shared=row.via_shared,
        content_type=row.content_type,
        created_at=row.created_at,
    )


def multipart_part_to_row(part: MultipartPart) -> m.MultipartPartRow:
    return m.MultipartPartRow(
        upload_id=part.upload_id,
        part_number=part.part_number,
        etag=part.etag,
        size=part.size,
        object_key=part.object_key,
        uploaded_at=part.uploaded_at,
    )


def multipart_part_from_row(row: m.MultipartPartRow) -> MultipartPart:
    return MultipartPart(
        upload_id=row.upload_id,
        part_number=row.part_number,
        etag=row.etag,
        size=row.size,
        object_key=row.object_key,
        uploaded_at=row.uploaded_at,
    )


# --- quota -----------------------------------------------------------------


def quota_to_row(usage: QuotaUsage) -> m.QuotaUsageRow:
    return m.QuotaUsageRow(
        user_id=usage.user_id,
        live_bytes=usage.live_bytes,
        trashed_bytes=usage.trashed_bytes,
        version_bytes=usage.version_bytes,
        updated_at=usage.updated_at,
    )


def apply_quota(row: m.QuotaUsageRow, usage: QuotaUsage) -> None:
    row.live_bytes = usage.live_bytes
    row.trashed_bytes = usage.trashed_bytes
    row.version_bytes = usage.version_bytes
    row.updated_at = usage.updated_at


def quota_from_row(row: m.QuotaUsageRow) -> QuotaUsage:
    return QuotaUsage(
        user_id=row.user_id,
        live_bytes=row.live_bytes,
        trashed_bytes=row.trashed_bytes,
        version_bytes=row.version_bytes,
        updated_at=row.updated_at,
    )


# --- audit -----------------------------------------------------------------


def audit_to_row(record: AuditRecord) -> m.AuditRow:
    return m.AuditRow(
        action=str(record.action),
        occurred_at=record.occurred_at,
        protocol=str(record.protocol),
        actor_subject=record.actor_subject,
        target_id=record.target_id,
        recipient_subject=record.recipient_subject,
        reason=record.reason,
        source_ip=record.source_ip,
        context=dict(record.context) or None,
    )


def audit_from_row(row: m.AuditRow) -> AuditRecord:
    return AuditRecord(
        action=AuditAction(row.action),
        occurred_at=row.occurred_at,
        protocol=AuditProtocol(row.protocol),
        actor_subject=row.actor_subject,
        target_id=row.target_id,
        recipient_subject=row.recipient_subject,
        reason=row.reason,
        source_ip=row.source_ip,
        context=dict(row.context or {}),
    )


# --- backups ---------------------------------------------------------------


def backup_record_to_row(record: BackupRecord) -> m.BackupRecordRow:
    return m.BackupRecordRow(
        id=record.id,
        started_at=record.started_at,
        finished_at=record.finished_at,
        state=str(record.state),
        schema_revision=record.schema_revision,
        dump_checksum=record.dump_checksum,
        object_count=record.object_count,
        total_bytes=record.total_bytes,
        duration_seconds=record.duration_seconds,
        skew_missing_in_dump=record.skew_missing_in_dump,
        skew_missing_in_manifest=record.skew_missing_in_manifest,
        error=record.error,
    )


def apply_backup_record(row: m.BackupRecordRow, record: BackupRecord) -> None:
    row.finished_at = record.finished_at
    row.state = str(record.state)
    row.dump_checksum = record.dump_checksum
    row.object_count = record.object_count
    row.total_bytes = record.total_bytes
    row.duration_seconds = record.duration_seconds
    row.skew_missing_in_dump = record.skew_missing_in_dump
    row.skew_missing_in_manifest = record.skew_missing_in_manifest
    row.error = record.error


def backup_record_from_row(row: m.BackupRecordRow) -> BackupRecord:
    return BackupRecord(
        id=row.id,
        started_at=row.started_at,
        state=BackupState(row.state),
        schema_revision=row.schema_revision,
        finished_at=row.finished_at,
        dump_checksum=row.dump_checksum,
        object_count=row.object_count,
        total_bytes=row.total_bytes,
        skew_missing_in_dump=row.skew_missing_in_dump,
        skew_missing_in_manifest=row.skew_missing_in_manifest,
        error=row.error,
    )


# --- grants ----------------------------------------------------------------


def grant_to_row(grant: Grant) -> m.GrantRow:
    return m.GrantRow(
        id=grant.id,
        node_id=grant.node_id,
        subject=grant.subject,
        role=int(grant.role),
        granted_by=grant.granted_by,
        created_at=grant.created_at,
        updated_at=grant.updated_at,
        pending=grant.pending,
    )


def grant_from_row(row: m.GrantRow) -> Grant:
    return Grant(
        id=row.id,
        node_id=row.node_id,
        subject=row.subject,
        role=Role(row.role),
        granted_by=row.granted_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        pending=row.pending,
    )


# --- public links ----------------------------------------------------------


def link_to_row(link: PublicLink) -> m.PublicLinkRow:
    return m.PublicLinkRow(
        id=link.id,
        node_id=link.node_id,
        token_hash=link.token_hash,
        passphrase_hash=link.passphrase_hash,
        created_by=link.created_by,
        created_at=link.created_at,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        access_count=link.access_count,
        last_accessed_at=link.last_accessed_at,
    )


def apply_link(row: m.PublicLinkRow, link: PublicLink) -> None:
    row.revoked_at = link.revoked_at
    row.access_count = link.access_count
    row.last_accessed_at = link.last_accessed_at


def link_from_row(row: m.PublicLinkRow) -> PublicLink:
    return PublicLink(
        id=row.id,
        node_id=row.node_id,
        token_hash=row.token_hash,
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        passphrase_hash=row.passphrase_hash,
        revoked_at=row.revoked_at,
        access_count=row.access_count,
        last_accessed_at=row.last_accessed_at,
    )
