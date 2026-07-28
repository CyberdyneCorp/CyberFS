"""Optional content encryption.

Encryption is opt-in per file and inherited from the parent folder. When it is
on, content is sealed with a per-file data key, itself wrapped under the
owner's key-encryption key, itself wrapped under the deployment master key.

The property that matters: the set of principals who can obtain a file's DEK is
exactly the set authorized to read it. Sharing rewraps key material; it never
decrypts content to storage. Revoking deletes a wrapped key; it never rewrites
an object.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import KeyUnavailableError, NotFoundError
from cyberfs.domain.framing import FrameSpan, frames_for_range
from cyberfs.domain.keys import UserKey, WrappedDataKey
from cyberfs.domain.nodes import EncryptionDefault, Node
from cyberfs.domain.ports.crypto import ContentCipher, KeyProvider
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.infrastructure.logging import get_logger

ANCESTOR_GUARD_DEPTH = 512

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RotationResult:
    scanned: int = 0
    rewrapped: int = 0


def resolve_encryption(
    ancestors: tuple[Node, ...], *, requested: bool | None, default_on: bool
) -> bool:
    """Whether a file created here should be encrypted.

    Precedence: an explicit request wins; otherwise the nearest ancestor that
    states `on` or `off` decides; otherwise the deployment default. Ancestors
    arrive root-first, so the nearest is the last one that expresses an opinion.
    """
    if requested is not None:
        return requested
    for node in reversed(ancestors):
        if node.encryption_default is EncryptionDefault.ON:
            return True
        if node.encryption_default is EncryptionDefault.OFF:
            return False
    return default_on


class EncryptionService:
    """Implements `KeyRewrapper` for sharing, and seals content for storage."""

    def __init__(
        self,
        keys: KeyProvider,
        cipher: ContentCipher,
        *,
        encryption_default_on: bool = False,
    ) -> None:
        self._keys = keys
        self._cipher = cipher
        self._default_on = encryption_default_on

    @property
    def frame_bytes(self) -> int:
        return self._cipher.frame_bytes

    # --- policy --------------------------------------------------------

    async def should_encrypt(
        self, uow: UnitOfWork, parent: Node, *, requested: bool | None = None
    ) -> bool:
        chain = await uow.nodes.ancestors(parent.id, max_depth=ANCESTOR_GUARD_DEPTH)
        return resolve_encryption(
            (*chain, parent), requested=requested, default_on=self._default_on
        )

    # --- keys ----------------------------------------------------------

    async def _user_key(self, uow: UnitOfWork, user_id: uuid.UUID) -> bytes:
        """Unwrap a user's KEK. Exists in memory only for this request."""
        stored = await uow.keys.get_user_key(user_id)
        if stored is None:
            raise KeyUnavailableError("the user has no key-encryption key")
        return self._keys.unwrap_kek(stored.wrapped_kek, master_key_id=stored.master_key_id)

    async def create_data_key(
        self, uow: UnitOfWork, node: Node, subject: str, now: datetime
    ) -> bytes:
        """Mint a fresh DEK for a file and store it wrapped for its owner."""
        kek = await self._user_key(uow, node.owner_id)
        dek = self._cipher.generate_key()
        await uow.keys.add_data_key(
            WrappedDataKey(
                id=uuid.uuid4(),
                node_id=node.id,
                subject=subject,
                wrapped_dek=self._keys.wrap_dek(dek, kek),
                created_at=now,
            )
        )
        return dek

    async def ensure_data_key(
        self, uow: UnitOfWork, node: Node, subject: str, now: datetime
    ) -> bytes:
        """The node's DEK: minted on first use, reused by every later version.

        `wrapped_data_keys` is unique on `(node_id, subject)` and carries no
        version column, so a second version cannot bring a key of its own -- the
        insert collides on `uq_wrapped_data_keys_node_subject` and the whole write
        fails with a `409`. That is what made replacing the content of an
        encrypted file impossible from the day encryption landed.

        Reuse is also the only option that keeps history readable: a replacement
        key would leave every earlier version's bytes sealed under a DEK nothing
        stores any more.

        Versions stay cryptographically separated regardless, because `seal`
        mixes the version id into the AEAD -- a frame lifted out of one version
        cannot be replayed into another even though both are sealed under this
        one key.
        """
        wrapped = await uow.keys.get_data_key(node.id, subject)
        if wrapped is None:
            return await self.create_data_key(uow, node, subject, now)
        return self._keys.unwrap_dek(wrapped.wrapped_dek, await self._user_key(uow, node.owner_id))

    async def data_key_for(self, uow: UnitOfWork, node: Node, subject: str) -> bytes:
        """The DEK as `subject` can obtain it.

        A caller with no wrapped copy simply has no key -- which is what makes
        revocation effective even against a replayed request.
        """
        wrapped = await uow.keys.get_data_key(node.id, subject)
        if wrapped is None:
            raise KeyUnavailableError("no key is wrapped for this caller")
        user = await uow.users.get_by_subject(subject)
        if user is None:
            raise KeyUnavailableError("unknown subject")
        return self._keys.unwrap_dek(wrapped.wrapped_dek, await self._user_key(uow, user.id))

    # --- KeyRewrapper --------------------------------------------------

    async def rewrap_for(self, uow: UnitOfWork, node: Node, subject: str, now: datetime) -> None:
        """Give `subject` their own wrapped copy of this file's DEK.

        The content objects are untouched: sharing moves key material only.
        """
        if not node.encrypted:
            return
        if await uow.keys.get_data_key(node.id, subject) is not None:
            return

        owner = await uow.users.get(node.owner_id)
        if owner is None:
            raise KeyUnavailableError("the node has no owner record")
        dek = await self.data_key_for(uow, node, owner.subject)

        recipient = await uow.users.get_by_subject(subject)
        if recipient is None:
            # The recipient has never touched CyberFS, so has no KEK to wrap
            # under. Refusing beats storing a key nobody can open.
            raise KeyUnavailableError("the recipient has no key-encryption key yet")
        await uow.keys.add_data_key(
            WrappedDataKey(
                id=uuid.uuid4(),
                node_id=node.id,
                subject=subject,
                wrapped_dek=self._keys.wrap_dek(dek, await self._user_key(uow, recipient.id)),
                created_at=now,
            )
        )

    async def revoke_for(self, uow: UnitOfWork, node: Node, subject: str) -> None:
        await uow.keys.delete_data_key(node.id, subject)

    # --- content -------------------------------------------------------

    async def seal(
        self, plaintext: AsyncIterator[bytes], dek: bytes, version_id: uuid.UUID
    ) -> AsyncIterator[bytes]:
        return self._cipher.seal(plaintext, dek, version_id.bytes)

    def open(
        self,
        ciphertext: AsyncIterator[bytes],
        dek: bytes,
        version_id: uuid.UUID,
        *,
        span: FrameSpan | None = None,
    ) -> AsyncIterator[bytes]:
        return self._cipher.open(ciphertext, dek, version_id.bytes, span=span)

    def plan_range(self, start: int, length: int, plaintext_bytes: int) -> FrameSpan:
        """Which frames hold a plaintext range, so only those are fetched."""
        return frames_for_range(
            start,
            length,
            frame_bytes=self._cipher.frame_bytes,
            plaintext_bytes=plaintext_bytes,
        )

    def ciphertext_length(self, plaintext_bytes: int) -> int:
        return self._cipher.ciphertext_length(plaintext_bytes)

    async def verify_master_key(self, uow: UnitOfWork) -> bool:
        """Whether the configured master key can still open stored material.

        `content-encryption/spec.md` requires readiness to fail when the key
        cannot open what is stored, rather than serving a 500 per file. A
        deployment with no wrapped keys yet trivially passes.
        """
        page = await uow.users.list_all(limit=1)
        if not page.items:
            return True
        stored = await uow.keys.get_user_key(page.items[0].id)
        if stored is None:
            return True
        try:
            self._keys.unwrap_kek(stored.wrapped_kek, master_key_id=stored.master_key_id)
        except KeyUnavailableError:
            logger.error("master_key_cannot_open_stored_keys")
            return False
        return True

    # --- rotation ------------------------------------------------------

    async def rotate_master_key(
        self, uow: UnitOfWork, *, batch: int = 100, now: datetime | None = None
    ) -> RotationResult:
        """Rewrap every KEK under the current master key.

        Content objects are never touched -- only the top of the envelope
        moves. Resumable by construction: each pass looks for keys still sealed
        under an older master, so rerunning finishes what an interruption left.
        """
        moment = now or utcnow()
        current = self._keys.master_key_id
        scanned = rewrapped = 0

        for stored in await self._all_stale_user_keys(uow, current, batch):
            scanned += 1
            kek = self._keys.unwrap_kek(stored.wrapped_kek, master_key_id=stored.master_key_id)
            await uow.keys.update_user_key(
                stored.rewrapped(self._keys.wrap_kek(kek), current, moment)
            )
            rewrapped += 1

        if rewrapped:
            await uow.audit.add(
                AuditRecord(
                    action=AuditAction.KEY_ROTATED,
                    occurred_at=moment,
                    context={"master_key_id": current, "keys_rewrapped": rewrapped},
                )
            )
        await uow.commit()
        logger.info("master_key_rotation", scanned=scanned, rewrapped=rewrapped)
        return RotationResult(scanned, rewrapped)

    @staticmethod
    async def _all_stale_user_keys(
        uow: UnitOfWork, current: str, batch: int
    ) -> tuple[UserKey, ...]:
        """Every KEK not already sealed under the current master key."""
        page = await uow.users.list_all(limit=batch)
        stale: list[UserKey] = []
        for user in page.items:
            key = await uow.keys.get_user_key(user.id)
            if key is not None and key.master_key_id != current:
                stale.append(key)
        return tuple(stale)

    async def rotate_user_key(
        self, uow: UnitOfWork, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> RotationResult:
        """Replace a user's KEK, rewrapping every DEK sealed under the old one."""
        moment = now or utcnow()
        stored = await uow.keys.get_user_key(user_id)
        if stored is None:
            raise NotFoundError("the user has no key-encryption key")
        user = await uow.users.get(user_id)
        if user is None:
            raise NotFoundError("no such user")

        old_kek = self._keys.unwrap_kek(stored.wrapped_kek, master_key_id=stored.master_key_id)
        new_kek = self._keys.generate_kek()

        rewrapped = 0
        for node in await self._encrypted_nodes_for(uow, user_id):
            wrapped = await uow.keys.get_data_key(node.id, user.subject)
            if wrapped is None:
                continue
            dek = self._keys.unwrap_dek(wrapped.wrapped_dek, old_kek)
            await uow.keys.delete_data_key(node.id, user.subject)
            await uow.keys.add_data_key(
                WrappedDataKey(
                    id=uuid.uuid4(),
                    node_id=node.id,
                    subject=user.subject,
                    wrapped_dek=self._keys.wrap_dek(dek, new_kek),
                    created_at=moment,
                )
            )
            rewrapped += 1

        # Only once every DEK has moved: the old KEK stays valid until then, so
        # an interruption leaves everything still openable.
        await uow.keys.update_user_key(
            stored.rewrapped(self._keys.wrap_kek(new_kek), self._keys.master_key_id, moment)
        )
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.KEY_ROTATED,
                occurred_at=moment,
                actor_subject=user.subject,
                target_id=str(user_id),
                context={"data_keys_rewrapped": rewrapped},
            )
        )
        await uow.flush()
        return RotationResult(rewrapped, rewrapped)

    @staticmethod
    async def _encrypted_nodes_for(uow: UnitOfWork, user_id: uuid.UUID) -> tuple[Node, ...]:
        user = await uow.users.get(user_id)
        if user is None:
            return ()
        subtree = await uow.nodes.descendants(
            user.root_folder_id, max_depth=ANCESTOR_GUARD_DEPTH, include_deleted=True
        )
        return tuple(node for node in subtree if node.encrypted)
