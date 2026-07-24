"""Grants, public links, and ownership transfer.

Only an owner may widen access: an editor can change a file's contents but
never who else can see it. Every change here is audited, and every one that
touches encrypted content rewraps key material inside the same transaction --
a grant that is visible but whose recipient cannot decrypt would be worse than
no grant at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from cyberfs.application.caching import CacheService
from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import (
    CannotRevokeOwnerError,
    CannotShareWithSelfError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitedError,
    RecipientUnknownError,
    ValidationError,
)
from cyberfs.domain.links import (
    generate_token,
    hash_passphrase,
    hash_token,
    verify_passphrase,
)
from cyberfs.domain.nodes import Node
from cyberfs.domain.permissions import require_role, resolve_effective_role
from cyberfs.domain.ports.identity import UserDirectory
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.domain.sharing import Grant, PublicLink, Role
from cyberfs.domain.users import User
from cyberfs.infrastructure.logging import get_logger

ANCESTOR_GUARD_DEPTH = 512
PASSPHRASE_WINDOW = timedelta(minutes=1)

logger = get_logger(__name__)


class KeyRewrapper(Protocol):
    """Rewraps a file's data key for another subject.

    Implemented by the encryption capability. Sharing an encrypted file means
    rewrapping its DEK under the recipient's KEK -- the content objects are
    never touched, and never decrypted.
    """

    async def rewrap_for(
        self, uow: UnitOfWork, node: Node, subject: str, now: datetime
    ) -> None: ...

    async def revoke_for(self, uow: UnitOfWork, node: Node, subject: str) -> None: ...


@dataclass(frozen=True, slots=True)
class IssuedLink:
    """A newly created link. The token is returned exactly once."""

    link: PublicLink
    token: str


@dataclass(frozen=True, slots=True)
class LinkAccess:
    """A resolved public link and the node it opens."""

    link: PublicLink
    node: Node
    role: Role = Role.VIEWER


class SharingService:
    def __init__(
        self,
        directory: UserDirectory,
        *,
        keys: KeyRewrapper | None = None,
        cache: CacheService | None = None,
        passphrase_attempts_per_min: int = 10,
    ) -> None:
        self._directory = directory
        self._keys = keys
        self._cache = cache
        self._attempts = FixedWindowLimiter(
            limit=passphrase_attempts_per_min, window=PASSPHRASE_WINDOW
        )

    # --- grants --------------------------------------------------------

    async def grant(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        recipient: str,
        role: Role,
        *,
        now: datetime | None = None,
    ) -> Grant:
        moment = now or utcnow()
        node = await self._require_owned(uow, user, node_id)

        subject = await self._resolve_recipient(user, recipient)
        if subject == user.subject:
            raise CannotShareWithSelfError()

        existing = await uow.grants.find(node.id, subject)
        grant = (
            existing.with_role(role, moment)
            if existing is not None
            else Grant(
                id=uuid.uuid4(),
                node_id=node.id,
                subject=subject,
                role=role,
                granted_by=user.subject,
                created_at=moment,
                updated_at=moment,
            )
        )
        if existing is not None:
            await uow.grants.update(grant)
        else:
            await uow.grants.add(grant)

        # In the same transaction: a grant whose key rewrap failed would be
        # visible but unusable.
        await self._rewrap_subtree(uow, node, subject, moment)
        await self._audit(
            uow,
            AuditAction.GRANT_UPDATED if existing else AuditAction.GRANT_CREATED,
            moment,
            actor=user.subject,
            target=node.id,
            recipient=subject,
            role=role,
        )
        await self._invalidate(subject)
        await uow.flush()
        return grant

    async def revoke(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        subject: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Remove a grant. Either the node's owner or the recipient may do so."""
        moment = now or utcnow()
        node = await self._require_node(uow, node_id)
        is_owner = node.owner_id == user.id
        if not is_owner and subject != user.subject:
            # A recipient may drop their own access, and nothing else.
            raise PermissionDeniedError("only the owner may revoke another user's access")

        owner = await uow.users.get(node.owner_id)
        if owner is not None and subject == owner.subject:
            raise CannotRevokeOwnerError()

        grant = await uow.grants.find(node.id, subject)
        if grant is None:
            raise NotFoundError("no such grant", node_id=str(node_id))
        await uow.grants.delete(grant.id)

        # The recipient's wrapped keys go too, so a replayed request finds
        # nothing to unwrap.
        await self._revoke_keys(uow, node, subject)
        await self._audit(
            uow,
            AuditAction.GRANT_REVOKED,
            moment,
            actor=user.subject,
            target=node.id,
            recipient=subject,
        )
        # Before the response, never on a TTL: `caching/spec.md` requires the
        # recipient's very next request to be denied.
        await self._invalidate(subject)
        await uow.flush()

    async def list_grants(
        self, uow: UnitOfWork, user: User, node_id: uuid.UUID
    ) -> tuple[Grant, ...]:
        node = await self._require_owned(uow, user, node_id)
        return await uow.grants.list_for_node(node.id)

    async def shared_with_me(self, uow: UnitOfWork, user: User) -> tuple[Node, ...]:
        """The roots of each shared subtree, not every inherited descendant."""
        grants = await uow.grants.list_for_subject(user.subject)
        granted_ids = {g.node_id for g in grants}

        roots: list[Node] = []
        for grant in grants:
            node = await uow.nodes.get(grant.node_id)
            if node is None or node.is_deleted:
                continue
            chain = await uow.nodes.ancestors(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
            # Skip anything already covered by a grant further up: listing both
            # a folder and a file inside it would be noise.
            if any(ancestor.id in granted_ids for ancestor in chain):
                continue
            roots.append(node)
        return tuple(roots)

    # --- public links --------------------------------------------------

    async def create_link(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        expires_at: datetime | None = None,
        passphrase: str | None = None,
        now: datetime | None = None,
    ) -> IssuedLink:
        moment = now or utcnow()
        node = await self._require_owned(uow, user, node_id)
        if expires_at is not None and expires_at <= moment:
            raise ValidationError("expiry must be in the future")

        token = generate_token()
        link = PublicLink(
            id=uuid.uuid4(),
            node_id=node.id,
            token_hash=hash_token(token),
            created_by=user.subject,
            created_at=moment,
            expires_at=expires_at,
            passphrase_hash=hash_passphrase(passphrase) if passphrase else None,
        )
        await uow.public_links.add(link)
        await self._audit(
            uow,
            AuditAction.PUBLIC_LINK_CREATED,
            moment,
            actor=user.subject,
            target=node.id,
        )
        await uow.flush()
        # The only time the token exists in cleartext outside the client.
        return IssuedLink(link=link, token=token)

    async def resolve_link(
        self,
        uow: UnitOfWork,
        token: str,
        *,
        passphrase: str | None = None,
        source_ip: str | None = None,
        now: datetime | None = None,
    ) -> LinkAccess:
        moment = now or utcnow()
        link = await uow.public_links.get_by_token_hash(hash_token(token))
        # Revoked, expired, and never-existed are one response: none of them
        # should reveal that a link was ever there.
        if link is None or not link.is_usable(moment):
            raise NotFoundError("no such link")

        if link.requires_passphrase:
            self._check_passphrase(link, passphrase, source_ip, moment)

        node = await uow.nodes.get(link.node_id)
        if node is None or node.is_deleted:
            raise NotFoundError("no such link")

        link.record_access(moment)
        await uow.public_links.update(link)
        await self._audit(
            uow,
            AuditAction.PUBLIC_LINK_ACCESSED,
            moment,
            target=node.id,
            source_ip=source_ip,
            link_id=str(link.id),
        )
        return LinkAccess(link=link, node=node)

    def _check_passphrase(
        self,
        link: PublicLink,
        passphrase: str | None,
        source_ip: str | None,
        now: datetime,
    ) -> None:
        """Rate limited per link, so a weak passphrase cannot be ground down."""
        key = f"{link.id}:{source_ip or 'anonymous'}"
        if self._attempts.is_limited(key, now):
            raise RateLimitedError(
                "too many passphrase attempts",
                retry_after_seconds=int(self._attempts.retry_after(key, now).total_seconds()),
            )
        if passphrase is None or not verify_passphrase(passphrase, link.passphrase_hash or ""):
            self._attempts.record(key, now)
            raise PermissionDeniedError("the link passphrase is incorrect")

    async def revoke_link(
        self,
        uow: UnitOfWork,
        user: User,
        link_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> None:
        moment = now or utcnow()
        link = await uow.public_links.get(link_id)
        if link is None:
            raise NotFoundError("no such link")
        node = await self._require_owned(uow, user, link.node_id)

        link.revoke(moment)
        await uow.public_links.update(link)
        await self._audit(
            uow,
            AuditAction.PUBLIC_LINK_REVOKED,
            moment,
            actor=user.subject,
            target=node.id,
            link_id=str(link.id),
        )
        await uow.flush()

    async def list_links(
        self, uow: UnitOfWork, user: User, node_id: uuid.UUID
    ) -> tuple[PublicLink, ...]:
        node = await self._require_owned(uow, user, node_id)
        return await uow.public_links.list_for_node(node.id)

    # --- ownership -----------------------------------------------------

    async def transfer_ownership(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        recipient: str,
        *,
        keep_editor_access: bool = True,
        now: datetime | None = None,
    ) -> Node:
        moment = now or utcnow()
        node = await self._require_owned(uow, user, node_id)
        subject = await self._resolve_recipient(user, recipient)
        if subject == user.subject:
            raise CannotShareWithSelfError()

        new_owner = await uow.users.get_by_subject(subject)
        if new_owner is None:
            # The recipient has no CyberFS record yet, so no quota to charge.
            raise RecipientUnknownError("the recipient has never used CyberFS")

        subtree = await uow.nodes.descendants(
            node.id, max_depth=ANCESTOR_GUARD_DEPTH, include_deleted=True
        )
        moving = (node, *subtree)
        size = sum(n.size_bytes for n in moving if n.is_file)
        await self._move_quota(uow, user, new_owner, size, moment)

        for item in moving:
            # Rewrap before the ownership flips, so a failure aborts the whole
            # transfer rather than leaving unreadable files behind.
            await self._rewrap_one(uow, item, subject, moment)
            item.owner_id = new_owner.id
            item.touch(moment)
            await uow.nodes.update(item)

        if keep_editor_access:
            await uow.grants.add(
                Grant(
                    id=uuid.uuid4(),
                    node_id=node.id,
                    subject=user.subject,
                    role=Role.EDITOR,
                    granted_by=user.subject,
                    created_at=moment,
                    updated_at=moment,
                )
            )
            await self._rewrap_subtree(uow, node, user.subject, moment)

        await self._audit(
            uow,
            AuditAction.OWNERSHIP_TRANSFERRED,
            moment,
            actor=user.subject,
            target=node.id,
            recipient=subject,
            bytes_moved=size,
        )
        # Ownership drives permission, so both parties' decisions are stale.
        await self._invalidate(subject)
        await self._invalidate(user.subject)
        await uow.flush()
        return node

    @staticmethod
    async def _move_quota(
        uow: UnitOfWork, previous: User, new_owner: User, size: int, now: datetime
    ) -> None:
        """Charge the recipient and release the sender, or refuse outright."""
        if size <= 0:
            return
        target = await uow.quotas.get(new_owner.id)
        if target is None:
            raise QuotaExceededError("the recipient has no quota record")
        if target.would_exceed(new_owner.quota_bytes, size):
            raise QuotaExceededError(
                "the recipient cannot accommodate the transferred subtree",
                quota_bytes=new_owner.quota_bytes,
                requested_bytes=size,
            )
        target.charge_live(size, now)
        await uow.quotas.update(target)

        source = await uow.quotas.get(previous.id)
        if source is not None:
            source.charge_live(-size, now)
            await uow.quotas.update(source)

    # --- key material --------------------------------------------------

    async def _rewrap_subtree(
        self, uow: UnitOfWork, node: Node, subject: str, now: datetime
    ) -> None:
        await self._rewrap_one(uow, node, subject, now)
        for descendant in await uow.nodes.descendants(node.id, max_depth=ANCESTOR_GUARD_DEPTH):
            await self._rewrap_one(uow, descendant, subject, now)

    async def _rewrap_one(self, uow: UnitOfWork, node: Node, subject: str, now: datetime) -> None:
        if not node.encrypted:
            return
        if self._keys is None:
            # Fail closed: granting access to content the recipient could never
            # decrypt would be a silently broken share.
            raise PermissionDeniedError(
                "encrypted content cannot be shared without the key service",
                node_id=str(node.id),
            )
        await self._keys.rewrap_for(uow, node, subject, now)

    async def _revoke_keys(self, uow: UnitOfWork, node: Node, subject: str) -> None:
        if self._keys is None:
            await uow.keys.delete_data_key(node.id, subject)
            return
        await self._keys.revoke_for(uow, node, subject)
        for descendant in await uow.nodes.descendants(node.id, max_depth=ANCESTOR_GUARD_DEPTH):
            await self._keys.revoke_for(uow, descendant, subject)

    async def _invalidate(self, subject: str) -> None:
        """Drop every cached permission decision for one subject.

        Cheaper and safer than enumerating an arbitrarily large subtree, and
        the blast radius is one caller's decisions.
        """
        if self._cache is not None:
            await self._cache.invalidate_permissions_for_subject(subject)

    # --- helpers -------------------------------------------------------

    async def _resolve_recipient(self, user: User, identifier: str) -> str:
        subject = await self._directory.find_subject(
            identifier, within_orgs=[org.id for org in user.orgs]
        )
        if subject is None:
            raise RecipientUnknownError("no such user", identifier=identifier)
        return subject

    @staticmethod
    async def _require_node(uow: UnitOfWork, node_id: uuid.UUID) -> Node:
        node = await uow.nodes.get(node_id)
        if node is None or node.is_deleted:
            raise NotFoundError("node not found", node_id=str(node_id))
        return node

    async def _require_owned(self, uow: UnitOfWork, user: User, node_id: uuid.UUID) -> Node:
        """Sharing requires `owner`; an editor cannot widen access."""
        node = await self._require_node(uow, node_id)
        chain = await uow.nodes.ancestors(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
        granted = await uow.grants.highest_role_over(
            user.subject, [node.id, *(a.id for a in chain)]
        )
        role = resolve_effective_role(
            is_owner=node.owner_id == user.id,
            granted=(granted,) if granted is not None else (),
        )
        require_role(role, Role.OWNER, node_id=node_id)
        return node

    @staticmethod
    async def _audit(
        uow: UnitOfWork,
        action: AuditAction,
        now: datetime,
        *,
        actor: str | None = None,
        target: uuid.UUID | None = None,
        recipient: str | None = None,
        role: Role | None = None,
        source_ip: str | None = None,
        **context: object,
    ) -> None:
        await uow.audit.add(
            AuditRecord(
                action=action,
                occurred_at=now,
                actor_subject=actor,
                target_id=str(target) if target else None,
                recipient_subject=recipient,
                source_ip=source_ip,
                context={**context, **({"role": role.slug} if role else {})},
            )
        )
