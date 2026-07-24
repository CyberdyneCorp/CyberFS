"""Per-request Unit of Work.

One transaction per request, opened and closed by the application layer, so a
use case's writes land together or not at all -- a grant and its key rewrap, a
purge and the quota it releases.
"""

from __future__ import annotations

import uuid
from types import TracebackType

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cyberfs.adapters.outbound.db.admin_queries import SqlAdminQueries
from cyberfs.adapters.outbound.db.repositories import (
    SqlAuditRepository,
    SqlFileVersionRepository,
    SqlGrantRepository,
    SqlKeyRepository,
    SqlNodeRepository,
    SqlPublicLinkRepository,
    SqlQuotaRepository,
    SqlUserRepository,
)
from cyberfs.domain.errors import ConflictError, CyberFSError, NameTakenError

#: Namespace for subtree locks, keeping them clear of the migration lock.
SUBTREE_LOCK_NAMESPACE = 0x0C_1B_F5

#: Database constraints are the real guarantee under concurrency -- a
#: read-then-write check can always be raced. Their violations are translated
#: here so the application layer sees domain errors and never SQLAlchemy.
_CONSTRAINT_ERRORS: dict[str, type[CyberFSError]] = {
    "uq_nodes_parent_name_live": NameTakenError,
    "uq_grants_node_subject": ConflictError,
    "uq_wrapped_data_keys_node_subject": ConflictError,
    "uq_file_versions_node_sequence": ConflictError,
}


def translate_integrity_error(exc: IntegrityError) -> CyberFSError | None:
    """Map a constraint violation to its domain error, if we know it."""
    detail = str(exc.orig) if exc.orig is not None else str(exc)
    for constraint, error_type in _CONSTRAINT_ERRORS.items():
        if constraint in detail:
            return error_type()
    return None


class SqlUnitOfWork:
    """Implements the `UnitOfWork` port over one SQLAlchemy session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work is not active; use it as a context manager")
        return self._session

    async def __aenter__(self) -> SqlUnitOfWork:
        self._session = self._session_factory()
        session = self._session
        self.users = SqlUserRepository(session)
        self.nodes = SqlNodeRepository(session)
        self.versions = SqlFileVersionRepository(session)
        self.grants = SqlGrantRepository(session)
        self.keys = SqlKeyRepository(session)
        self.public_links = SqlPublicLinkRepository(session)
        self.quotas = SqlQuotaRepository(session)
        self.audit = SqlAuditRepository(session)
        self.admin = SqlAdminQueries(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            # Roll back on the way out unless the use case committed. An
            # un-committed transaction is a bug, not an implicit save.
            if exc_type is not None:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise (translate_integrity_error(exc) or ConflictError()) from exc

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        """Push pending writes so generated defaults and constraint violations
        surface before the use case continues.

        Also required where one row references another: the mapping declares no
        ORM relationships, so insert order is not derived automatically.
        """
        try:
            await self.session.flush()
        except IntegrityError as exc:
            translated = translate_integrity_error(exc)
            if translated is None:
                raise
            raise translated from exc

    async def lock_subtree(self, node_id: uuid.UUID) -> None:
        """Take a transaction-scoped advisory lock on a subtree.

        Two concurrent moves could each pass a cycle check and still create a
        cycle together; serializing them on the subtree root prevents that. The
        lock releases automatically at commit or rollback.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": SUBTREE_LOCK_NAMESPACE, "key": _lock_key(node_id)},
        )


def _lock_key(node_id: uuid.UUID) -> int:
    """Fold a UUID into the signed 32-bit space Postgres advisory locks use."""
    return (node_id.int % (2**31 - 1)) - (2**30)
