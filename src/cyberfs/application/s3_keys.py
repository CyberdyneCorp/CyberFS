"""S3 access-key lifecycle: mint, list, revoke.

The service that turns a request for a credential into a stored, sealed key --
and, once, the plaintext secret the caller must copy now because it is never
recoverable afterwards. The verifier (phase 4) reads these keys; last-used is
stamped there, on every authenticated request, so idle credentials surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import NotFoundError, PermissionDeniedError
from cyberfs.domain.ports.crypto import KeyProvider
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.s3.access_key import S3AccessKey, generate_key_id, generate_secret
from cyberfs.domain.users import User
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedS3Key:
    """A newly minted key. `secret` is the plaintext, returned exactly once and
    never stored -- only its sealed form lives on `key`."""

    key: S3AccessKey
    secret: str


class S3AccessKeyService:
    """Mints, lists, and revokes access-key credentials for a subject."""

    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    async def mint(
        self,
        uow: UnitOfWork,
        principal: Principal,
        owner: User,
        *,
        label: str,
        now: datetime | None = None,
    ) -> IssuedS3Key:
        """Create a key for `owner`, returning the plaintext secret once.

        A service principal owns no tree and so gets no key -- refused, not
        silently issued one. The secret is sealed under `MASTER_KEY` before it
        touches storage; the plaintext leaves this method only in the return
        value and never appears in a log or an audit record.
        """
        if principal.is_service:
            raise PermissionDeniedError(
                "a service principal cannot hold an access key",
                subject=principal.subject,
            )

        moment = now or utcnow()
        secret = generate_secret()
        key = S3AccessKey(
            key_id=generate_key_id(),
            sealed_secret=self._keys.seal_secret(secret.encode("utf-8")),
            secret_master_key_id=self._keys.master_key_id,
            label=label,
            owner_id=owner.id,
            owner_subject=owner.subject,
            created_at=moment,
        )
        await uow.s3_keys.add(key)
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.S3_KEY_CREATED,
                occurred_at=moment,
                actor_subject=owner.subject,
                target_id=key.key_id,
            )
        )
        await uow.flush()
        logger.info("s3_key_created", key_id=key.key_id, subject=owner.subject)
        # The only time the secret exists in cleartext outside the caller.
        return IssuedS3Key(key=key, secret=secret)

    async def list(self, uow: UnitOfWork, owner_subject: str) -> tuple[S3AccessKey, ...]:
        """Every key the subject owns. The secret is never part of this shape."""
        return await uow.s3_keys.list_for_subject(owner_subject)

    async def revoke(
        self,
        uow: UnitOfWork,
        owner_subject: str,
        key_id: str,
        *,
        now: datetime | None = None,
    ) -> S3AccessKey:
        """Revoke a key. Takes effect immediately -- the next signed request
        finds it inactive, with no cache to expire.

        A key that belongs to another subject is reported as not found, so the
        endpoint never confirms another user's key ids exist.
        """
        moment = now or utcnow()
        key = await self._require_owned(uow, owner_subject, key_id)
        revoked = key.revoked(moment)
        await uow.s3_keys.update(revoked)
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.S3_KEY_REVOKED,
                occurred_at=moment,
                actor_subject=owner_subject,
                target_id=key_id,
            )
        )
        logger.info("s3_key_revoked", key_id=key_id, subject=owner_subject)
        return revoked

    async def _require_owned(self, uow: UnitOfWork, owner_subject: str, key_id: str) -> S3AccessKey:
        key = await uow.s3_keys.get_by_key_id(key_id)
        if key is None or key.owner_subject != owner_subject:
            raise NotFoundError("access key not found", key_id=key_id)
        return key
