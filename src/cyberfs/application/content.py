"""File content: upload, download, versions.

Bytes always transit the API -- `file-storage/spec.md` forbids handing a client
a presigned URL -- so every path here streams in bounded chunks and computes
the plaintext digest in flight rather than re-reading what it just wrote.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from cyberfs.application.auditing import authorize_or_record, emit_audit, owner_context
from cyberfs.application.encryption import EncryptionService
from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import (
    IntegrityFailureError,
    KeyUnavailableError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from cyberfs.domain.nodes import FileVersion, Node, NodeKind, validate_name
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.ports.storage import ObjectStore
from cyberfs.domain.sharing import Role
from cyberfs.domain.users import User
from cyberfs.infrastructure.logging import get_logger
from cyberfs.infrastructure.metrics import (
    bytes_downloaded_total,
    bytes_uploaded_total,
    quota_rejections_total,
)

DEFAULT_CONTENT_TYPE = "application/octet-stream"

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ByteRange:
    """A resolved, satisfiable range within an object."""

    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length - 1


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    """Everything the transport needs to serve a download."""

    node: Node
    version: FileVersion
    stream: AsyncIterator[bytes]
    total_bytes: int
    served_bytes: int
    content_range: ByteRange | None

    @property
    def is_partial(self) -> bool:
        return self.content_range is not None


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """Parse a single-range `Range: bytes=…` header.

    Multi-range requests are declined by returning `None`, which serves the
    whole object -- a legal response, and far simpler than multipart/byteranges.
    """
    if not header or size <= 0:
        return None
    prefix, _, spec = header.partition("=")
    if prefix.strip().lower() != "bytes" or "," in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()

    if not first and last.isdigit():
        # A suffix range: the final N bytes.
        length = min(int(last), size)
        return ByteRange(size - length, length) if length else None
    if not first.isdigit():
        return None

    start = int(first)
    if start >= size:
        return None
    end = min(int(last), size - 1) if last.isdigit() else size - 1
    if end < start:
        return None
    return ByteRange(start, end - start + 1)


class ContentService:
    def __init__(
        self,
        objects: ObjectStore,
        *,
        max_upload_bytes: int,
        upload_chunk_bytes: int,
        version_retention_count: int,
        encryption: EncryptionService | None = None,
    ) -> None:
        self._objects = objects
        self._max_upload_bytes = max_upload_bytes
        self._chunk_bytes = upload_chunk_bytes
        self._retention = version_retention_count
        self._encryption = encryption

    # --- auditing ------------------------------------------------------

    @staticmethod
    async def _audit(
        uow: UnitOfWork,
        user: User,
        node: Node,
        action: AuditAction,
        when: datetime,
        *,
        byte_count: int | None = None,
    ) -> None:
        """Emit one file-operation record, non-blocking, name only when owned.

        The plaintext byte count is carried where one applies (upload,
        download); it feeds the activity summary without ever holding content.
        """
        context = owner_context(node, user.id)
        if byte_count is not None:
            context["bytes"] = byte_count
        await emit_audit(
            uow,
            AuditRecord(
                action=action,
                occurred_at=when,
                actor_subject=user.subject,
                target_id=str(node.id),
                context=context,
            ),
        )

    # --- upload --------------------------------------------------------

    async def upload(
        self,
        uow: UnitOfWork,
        user: User,
        parent_id: uuid.UUID,
        name: str,
        body: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        declared_length: int | None = None,
        encrypted: bool | None = None,
        now: datetime | None = None,
    ) -> Node:
        """Create a file, or add a version to the one already at `name`."""
        moment = now or utcnow()
        parent = await self._require_node(uow, parent_id)
        if not parent.is_folder:
            raise ValidationError("uploads go into a folder", node_id=str(parent_id))
        await self._require(uow, user, parent, Role.EDITOR)

        existing = await uow.nodes.get_child_by_name(parent.id, _normalized(name))
        if existing is not None and not existing.is_file:
            raise ValidationError("a folder already uses that name", node_id=str(existing.id))
        wants_encryption = False
        if existing is None:
            wants_encryption = await self._resolve_encryption(uow, parent, encrypted, user, moment)

        node = existing or await self._new_file(
            uow, parent, name, moment, encrypted=wants_encryption
        )
        stored = await self._store_version(
            uow, user, node, body, content_type, declared_length, moment
        )
        await self._audit(
            uow, user, stored, AuditAction.FILE_UPLOADED, moment, byte_count=stored.size_bytes
        )
        return stored

    async def replace(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        body: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        declared_length: int | None = None,
        now: datetime | None = None,
    ) -> Node:
        moment = now or utcnow()
        node = await self._require_node(uow, node_id)
        if not node.is_file:
            raise ValidationError("only a file has content", node_id=str(node_id))
        await self._require(uow, user, node, Role.EDITOR)
        stored = await self._store_version(
            uow, user, node, body, content_type, declared_length, moment
        )
        await self._audit(
            uow, user, stored, AuditAction.FILE_UPLOADED, moment, byte_count=stored.size_bytes
        )
        return stored

    async def _resolve_encryption(
        self,
        uow: UnitOfWork,
        parent: Node,
        requested: bool | None,
        user: User,
        now: datetime,
    ) -> bool:
        """Apply the opt-in and inheritance rules, auditing an override."""
        if self._encryption is None:
            return False
        decided: bool = await self._encryption.should_encrypt(uow, parent, requested=requested)
        inherited = await self._encryption.should_encrypt(uow, parent, requested=None)
        if requested is not None and decided != inherited:
            # An explicit choice that contradicts the folder's default is
            # recorded -- especially a downgrade.
            await uow.audit.add(
                AuditRecord(
                    action=AuditAction.ENCRYPTION_OVERRIDDEN,
                    occurred_at=now,
                    actor_subject=user.subject,
                    target_id=str(parent.id),
                    context={"requested": decided, "inherited": inherited},
                )
            )
        return decided

    async def _new_file(
        self, uow: UnitOfWork, parent: Node, name: str, now: datetime, *, encrypted: bool = False
    ) -> Node:
        node = Node(
            id=uuid.uuid4(),
            # Charged to the folder's owner, not the uploader: an editor
            # working in someone else's folder does not pay for it.
            owner_id=parent.owner_id,
            kind=NodeKind.FILE,
            name=validate_name(name),
            parent_id=parent.id,
            created_at=now,
            updated_at=now,
            encrypted=encrypted,
        )
        await uow.nodes.add(node)
        await uow.flush()
        return node

    async def _store_version(
        self,
        uow: UnitOfWork,
        user: User,
        node: Node,
        body: AsyncIterator[bytes],
        content_type: str | None,
        declared_length: int | None,
        now: datetime,
    ) -> Node:
        """Write the object first, then the metadata.

        Ordering matters: an interrupted upload must leave no visible file. A
        partial object with no row is garbage the reaper collects; a row with
        no object would be a broken file the user can see.
        """
        await self._check_admission(uow, node, declared_length)

        version_id = uuid.uuid4()
        key = f"{node.owner_id}/{node.id}/{version_id}"
        digest = hashlib.sha256()
        tally = _Tally()
        counted = _measure(body, digest, self._max_upload_bytes, tally)

        payload = await self._seal_if_needed(uow, node, counted, version_id, now)
        await self._objects.put(key, payload, content_type=content_type or DEFAULT_CONTENT_TYPE)
        # Plaintext bytes throughout: what the caller sent, what a download
        # returns, and what the quota charges. Ciphertext overhead is under
        # 0.05% and is deliberately not billed.
        stored = tally.total
        if declared_length is not None and stored != declared_length:
            # The client lied or the connection truncated; either way the
            # object is wrong, so it goes away and nothing is recorded.
            await self._objects.delete(key)
            raise ValidationError(
                "body length did not match Content-Length",
                declared=declared_length,
                received=stored,
            )

        await self._charge(uow, node, stored, now)
        version = FileVersion(
            id=version_id,
            node_id=node.id,
            owner_id=node.owner_id,
            sequence=await uow.versions.next_sequence(node.id),
            size_bytes=stored,
            plaintext_digest=digest.hexdigest(),
            content_type=content_type or DEFAULT_CONTENT_TYPE,
            encrypted=node.encrypted,
            created_at=now,
            created_by=user.subject,
            # Sealed here and now, so the bytes are bound to this row's own id.
            seal_version_id=version_id,
        )
        await uow.versions.add(version)

        node.current_version_id = version.id
        node.size_bytes = stored
        node.content_type = version.content_type
        node.touch(now)
        await uow.nodes.update(node)
        await uow.flush()

        await self._prune_versions(uow, node, now)
        bytes_uploaded_total.labels(encrypted=str(node.encrypted).lower()).inc(stored)
        return node

    async def _seal_if_needed(
        self,
        uow: UnitOfWork,
        node: Node,
        plaintext: AsyncIterator[bytes],
        version_id: uuid.UUID,
        now: datetime,
    ) -> AsyncIterator[bytes]:
        """Frame and seal the stream when the file is encrypted."""
        if not node.encrypted:
            return plaintext
        if self._encryption is None:
            raise KeyUnavailableError("encryption is not configured")
        owner = await uow.users.get(node.owner_id)
        if owner is None:
            raise KeyUnavailableError("the node has no owner record")
        # The node's DEK, minted on the first version and reused after: the
        # schema stores one wrapped key per (node, subject), so minting a second
        # one here collided on the unique constraint and made every content
        # replacement on an encrypted file fail with a 409. Replay across
        # versions is prevented by `seal` binding each frame to `version_id`,
        # not by handing each version its own key.
        dek = await self._encryption.ensure_data_key(uow, node, owner.subject, now)
        return await self._encryption.seal(plaintext, dek, version_id)

    async def _check_admission(
        self, uow: UnitOfWork, node: Node, declared_length: int | None
    ) -> None:
        """Refuse obviously-too-large uploads before a single byte is stored."""
        if declared_length is not None and declared_length > self._max_upload_bytes:
            raise PayloadTooLargeError(
                "upload exceeds the maximum size", limit=self._max_upload_bytes
            )
        if declared_length is None:
            return
        usage = await uow.quotas.get(node.owner_id)
        owner = await uow.users.get(node.owner_id)
        if usage is None or owner is None:
            return
        if usage.would_exceed(owner.quota_bytes, declared_length):
            quota_rejections_total.inc()
            usage.ensure_room_for(owner.quota_bytes, declared_length)

    async def _charge(self, uow: UnitOfWork, node: Node, stored: int, now: datetime) -> None:
        """Charge the owner, or discard what was just written."""
        usage = await uow.quotas.get(node.owner_id)
        owner = await uow.users.get(node.owner_id)
        if usage is None or owner is None:
            return
        # The previous version stops being current and becomes retained
        # history, so it moves buckets rather than being released.
        if node.current_version_id is not None:
            usage.charge_live(-node.size_bytes, now)
            usage.charge_versions(node.size_bytes, now)
        if usage.would_exceed(owner.quota_bytes, stored):
            quota_rejections_total.inc()
            usage.ensure_room_for(owner.quota_bytes, stored)
        usage.charge_live(stored, now)
        await uow.quotas.update(usage)

    async def _prune_versions(self, uow: UnitOfWork, node: Node, now: datetime) -> None:
        """Drop history past the retention count, releasing its bytes."""
        doomed = await uow.versions.prune_beyond(node.id, self._retention)
        if not doomed:
            return
        usage = await uow.quotas.get(node.owner_id)
        for version in doomed:
            await self._objects.delete(version.object_key)
            if usage is not None:
                usage.charge_versions(-version.size_bytes, now)
        if usage is not None:
            await uow.quotas.update(usage)
        await uow.flush()

    async def set_encryption(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        encrypted: bool,
        now: datetime | None = None,
    ) -> Node:
        """Convert a file between encrypted and plaintext.

        An explicit rewrite into a new version, never an in-place flip. Only an
        owner may do it, because turning encryption *off* lowers protection and
        `content-encryption/spec.md` forbids a silent downgrade.
        """
        moment = now or utcnow()
        node = await self._require_node(uow, node_id)
        if not node.is_file:
            raise ValidationError("only a file has content", node_id=str(node_id))
        await self._require(uow, user, node, Role.OWNER)
        if node.encrypted == encrypted:
            return node

        # Read the current content through the old setting, then write it back
        # under the new one. A failure part-way leaves the original intact
        # because the node only flips once the new version is committed.
        # The conversion read is machinery, not a user download, so it emits no
        # download record of its own.
        plan = await self.download(uow, user, node_id, record_audit=False)
        buffered = _replay(plan.stream)
        node.encrypted = encrypted
        stored = await self._store_version(
            uow, user, node, buffered, node.content_type, None, moment
        )

        if not encrypted:
            # The wrapped keys are useless now, and keeping them would leave
            # key material for content that no longer needs it.
            await uow.keys.delete_data_keys_for_node(node.id)
        await uow.audit.add(
            AuditRecord(
                action=(
                    AuditAction.ENCRYPTION_ENABLED if encrypted else AuditAction.ENCRYPTION_DISABLED
                ),
                occurred_at=moment,
                actor_subject=user.subject,
                target_id=str(node.id),
            )
        )
        return stored

    # --- download ------------------------------------------------------

    async def download(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        range_header: str | None = None,
        version_id: uuid.UUID | None = None,
        record_audit: bool = True,
        link_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> DownloadPlan:
        moment = now or utcnow()
        node = await self._require_node(uow, node_id)
        if not node.is_file:
            raise ValidationError("only a file has content", node_id=str(node_id))
        await self._require(uow, user, node, Role.VIEWER)

        version = await self._resolve_version(uow, node, version_id)
        wanted = parse_range(range_header, version.size_bytes)
        served = wanted.length if wanted else version.size_bytes

        if version.encrypted:
            guarded = await self._open_encrypted(uow, node, version, user, wanted)
        else:
            offset = wanted.start if wanted else 0
            length = wanted.length if wanted else None
            stream = self._objects.get(version.object_key, offset=offset, length=length)
            # A whole-object read is verifiable against the recorded digest; a
            # range read is not, so it is streamed without that check.
            guarded = stream if wanted else _verify(stream, version)
        bytes_downloaded_total.labels(encrypted=str(node.encrypted).lower()).inc(served)
        if record_audit:
            await self._audit_download(uow, user, node, served, moment, link_id)
        return DownloadPlan(
            node=node,
            version=version,
            stream=guarded,
            total_bytes=version.size_bytes,
            served_bytes=served,
            content_range=wanted,
        )

    @staticmethod
    async def _audit_download(
        uow: UnitOfWork,
        user: User,
        node: Node,
        served: int,
        when: datetime,
        link_id: uuid.UUID | None,
    ) -> None:
        """Record a read, attributing it to the link when there is no caller.

        A public-link read has no authenticated user, so it names the link and
        never the owner. Every read carries its plaintext byte count; the name
        appears only when the actor owns the node.
        """
        if link_id is not None:
            context: dict[str, object] = {"bytes": served, "link_id": str(link_id)}
            actor: str | None = None
        else:
            context = {"bytes": served, **owner_context(node, user.id)}
            actor = user.subject
        await emit_audit(
            uow,
            AuditRecord(
                action=AuditAction.FILE_DOWNLOADED,
                occurred_at=when,
                actor_subject=actor,
                target_id=str(node.id),
                context=context,
            ),
        )

    async def _open_encrypted(
        self,
        uow: UnitOfWork,
        node: Node,
        version: FileVersion,
        user: User,
        wanted: ByteRange | None,
    ) -> AsyncIterator[bytes]:
        """Decrypt frame by frame, fetching only the frames a range touches."""
        if self._encryption is None:
            raise KeyUnavailableError("encryption is not configured")
        dek = await self._encryption.data_key_for(uow, node, user.subject)

        if wanted is None:
            stream = self._objects.get(version.object_key)
            return self._encryption.open(stream, dek, version.seal_version_id)

        span = self._encryption.plan_range(wanted.start, wanted.length, version.size_bytes)
        offset, length = span.ciphertext_range(self._encryption.frame_bytes)
        stream = self._objects.get(version.object_key, offset=offset, length=length)
        return self._encryption.open(stream, dek, version.seal_version_id, span=span)

    async def list_versions(
        self, uow: UnitOfWork, user: User, node_id: uuid.UUID
    ) -> tuple[FileVersion, ...]:
        node = await self._require_node(uow, node_id)
        await self._require(uow, user, node, Role.VIEWER)
        return await uow.versions.list_for_node(node.id)

    async def restore_version(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> Node:
        """Bring an old version back as a *new* one, never deleting history."""
        moment = now or utcnow()
        node = await self._require_node(uow, node_id)
        await self._require(uow, user, node, Role.EDITOR)
        source = await self._resolve_version(uow, node, version_id)

        target_id = uuid.uuid4()
        target_key = f"{node.owner_id}/{node.id}/{target_id}"
        await self._objects.copy(source.object_key, target_key)

        await self._charge(uow, node, source.size_bytes, moment)
        version = FileVersion(
            id=target_id,
            node_id=node.id,
            owner_id=node.owner_id,
            sequence=await uow.versions.next_sequence(node.id),
            size_bytes=source.size_bytes,
            plaintext_digest=source.plaintext_digest,
            content_type=source.content_type,
            encrypted=source.encrypted,
            created_at=moment,
            created_by=user.subject,
            # The source's *sealing* id, not the source's own id. These are the
            # source's bytes, copied rather than re-sealed, so what opens them is
            # whatever opened the source -- and taking `source.seal_version_id`
            # rather than `source.id` is what makes a copy of a copy work, with
            # no chain to walk at read time.
            seal_version_id=source.seal_version_id,
        )
        await uow.versions.add(version)
        node.current_version_id = version.id
        node.size_bytes = version.size_bytes
        node.content_type = version.content_type
        node.touch(moment)
        await uow.nodes.update(node)
        await uow.flush()
        await self._prune_versions(uow, node, moment)
        await self._audit(uow, user, node, AuditAction.VERSION_RESTORED, moment)
        return node

    async def duplicate(self, uow: UnitOfWork, source: Node, target: Node, now: datetime) -> int:
        """Implements `ContentDuplicator` for the copy use case.

        Copies the object server-side -- so an encrypted file's ciphertext is
        copied as-is and never has to be opened -- and records the version that
        names it. Without that row the copy would have bytes nobody can reach.
        """
        original = await self._resolve_source_version(uow, source)
        if original is None:
            return 0

        version_id = uuid.uuid4()
        target_key = f"{target.owner_id}/{target.id}/{version_id}"
        copied = await self._objects.copy(original.object_key, target_key)

        await uow.versions.add(
            FileVersion(
                id=version_id,
                node_id=target.id,
                owner_id=target.owner_id,
                sequence=1,
                size_bytes=original.size_bytes,
                plaintext_digest=original.plaintext_digest,
                content_type=original.content_type,
                encrypted=original.encrypted,
                created_at=now,
                created_by=target.owner_id.hex,
                # Copied bytes keep the sealing id that opens them, as in
                # `restore_version`.
                seal_version_id=original.seal_version_id,
            )
        )
        if original.encrypted and self._encryption is not None:
            # The ciphertext was copied, not re-sealed, so the copy needs the
            # source's DEK wrapped against its own node id -- that is what
            # `data_key_for` looks up when the copy is read. Without it the copy
            # has bytes, a version row, and no way to open either.
            owner = await uow.users.get(target.owner_id)
            if owner is None:
                raise KeyUnavailableError("the copy has no owner record")
            await self._encryption.copy_data_key(uow, source, target, owner.subject, now)

        target.current_version_id = version_id
        target.content_type = original.content_type
        await uow.flush()
        return copied or original.size_bytes

    @staticmethod
    async def _resolve_source_version(uow: UnitOfWork, source: Node) -> FileVersion | None:
        if source.current_version_id is None:
            return None
        return await uow.versions.get(source.current_version_id)

    # --- helpers -------------------------------------------------------

    @staticmethod
    async def _require_node(uow: UnitOfWork, node_id: uuid.UUID) -> Node:
        node = await uow.nodes.get(node_id)
        if node is None or node.is_deleted:
            raise NotFoundError("node not found", node_id=str(node_id))
        return node

    @staticmethod
    async def _require(uow: UnitOfWork, user: User, node: Node, minimum: Role) -> Role:
        from cyberfs.application.nodes import ANCESTOR_GUARD_DEPTH
        from cyberfs.domain.permissions import resolve_effective_role

        chain = await uow.nodes.ancestors(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
        granted = await uow.grants.highest_role_over(
            user.subject, [node.id, *(a.id for a in chain)]
        )
        role = resolve_effective_role(
            is_owner=node.owner_id == user.id,
            granted=(granted,) if granted is not None else (),
        )
        return await authorize_or_record(
            uow,
            actor_subject=user.subject,
            node_id=node.id,
            effective=role,
            minimum=minimum,
        )

    @staticmethod
    async def _resolve_version(
        uow: UnitOfWork, node: Node, version_id: uuid.UUID | None
    ) -> FileVersion:
        wanted = version_id or node.current_version_id
        if wanted is None:
            raise NotFoundError("file has no content yet", node_id=str(node.id))
        version = await uow.versions.get(wanted)
        if version is None or version.node_id != node.id:
            raise NotFoundError("no such version", node_id=str(node.id))
        return version


def _normalized(name: str) -> str:
    from cyberfs.domain.nodes import normalize_name

    return normalize_name(name)


class _Tally:
    """Carries the plaintext byte count out of the streaming pipeline."""

    __slots__ = ("total",)

    def __init__(self) -> None:
        self.total = 0


async def _measure(
    body: AsyncIterator[bytes], digest: hashlib._Hash, limit: int, tally: _Tally
) -> AsyncIterator[bytes]:
    """Pass bytes through, hashing and counting, refusing an oversized body.

    Counting happens here, before any sealing, so the recorded size and digest
    describe the plaintext regardless of whether the file is encrypted. The
    limit is enforced mid-stream so a client cannot make the service store
    gigabytes before being told no.
    """
    async for chunk in body:
        tally.total += len(chunk)
        if tally.total > limit:
            raise PayloadTooLargeError("upload exceeds the maximum size", limit=limit)
        digest.update(chunk)
        yield chunk


async def _replay(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Re-emit a stream so it can be fed into a fresh write.

    Conversion is the one path that reads and writes the same content, and the
    read must complete against the old key before the new write begins.
    """
    async for chunk in source:
        yield chunk


async def _verify(stream: AsyncIterator[bytes], version: FileVersion) -> AsyncIterator[bytes]:
    """Re-hash a whole-object read and compare against what was recorded.

    A mismatch means stored bytes changed underneath us, which is an alert, not
    a client error.
    """
    digest = hashlib.sha256()
    async for chunk in stream:
        digest.update(chunk)
        yield chunk
    if digest.hexdigest() != version.plaintext_digest:
        logger.error("content_digest_mismatch", version_id=str(version.id))
        raise IntegrityFailureError(node_id=str(version.node_id))
