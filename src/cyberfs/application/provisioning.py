"""First-touch user provisioning.

A CyberdyneAuth subject becomes a CyberFS user the first time they make an
authenticated request. Everything they need to exist -- the record, a root
folder, a key, and a quota -- is created in one transaction, so a caller never
observes a half-provisioned account.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import PermissionDeniedError
from cyberfs.domain.keys import UserKey
from cyberfs.domain.nodes import Node, NodeKind
from cyberfs.domain.ports.crypto import KeyProvider
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.users import QuotaUsage, User
from cyberfs.infrastructure.logging import get_logger

#: Never shown; a user's tree is presented rooted at `/`.
ROOT_FOLDER_NAME = "root"

logger = get_logger(__name__)


class ProvisioningService:
    """Resolves a verified principal to a local user, creating it if needed."""

    def __init__(self, keys: KeyProvider, *, default_quota_bytes: int) -> None:
        self._keys = keys
        self._default_quota_bytes = default_quota_bytes

    async def resolve(
        self,
        uow: UnitOfWork,
        principal: Principal,
        *,
        now: datetime | None = None,
    ) -> User:
        """Return the local user for `principal`, provisioning on first touch.

        A service principal has no home tree and can never own a file, so it is
        refused rather than silently given one.
        """
        if principal.is_service:
            raise PermissionDeniedError(
                "a service principal has no storage of its own",
                subject=principal.subject,
            )

        moment = now or utcnow()
        existing = await uow.users.get_by_subject(principal.subject)
        if existing is not None:
            return await self._refresh(uow, existing, principal, moment)
        return await self._provision(uow, principal, moment)

    async def _refresh(
        self, uow: UnitOfWork, user: User, principal: Principal, now: datetime
    ) -> User:
        """Keep the mutable claims current: admin status and org membership."""
        user.refresh_from_claims(
            is_admin=principal.is_admin,
            org=principal.org,
            orgs=principal.orgs,
            now=now,
        )
        await uow.users.update(user)
        return user

    async def _provision(self, uow: UnitOfWork, principal: Principal, now: datetime) -> User:
        user_id = uuid.uuid4()
        root = Node(
            id=uuid.uuid4(),
            owner_id=user_id,
            kind=NodeKind.FOLDER,
            name=ROOT_FOLDER_NAME,
            parent_id=None,
            created_at=now,
            updated_at=now,
        )
        user = User(
            id=user_id,
            subject=principal.subject,
            root_folder_id=root.id,
            quota_bytes=self._default_quota_bytes,
            created_at=now,
            updated_at=now,
            is_admin=principal.is_admin,
            org=principal.org,
            orgs=principal.orgs,
            last_seen_at=now,
        )

        # The root folder, the key, and the quota row all reference the user,
        # and the mapping declares no ORM relationships to order inserts by --
        # so the user is flushed before anything that points at it.
        await uow.users.add(user)
        await uow.flush()

        await uow.nodes.add(root)
        await uow.keys.add_user_key(
            UserKey(
                user_id=user_id,
                # Sealed immediately; the raw KEK never leaves this expression.
                wrapped_kek=self._keys.wrap_kek(self._keys.generate_kek()),
                master_key_id=self._keys.master_key_id,
                created_at=now,
            )
        )
        await uow.quotas.add(QuotaUsage(user_id=user_id, updated_at=now))
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.USER_PROVISIONED,
                occurred_at=now,
                actor_subject=principal.subject,
                target_id=str(user_id),
            )
        )
        logger.info("user_provisioned", user_id=str(user_id))
        return user
