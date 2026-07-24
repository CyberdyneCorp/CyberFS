"""The administrative surface.

Metadata and aggregates only. There is deliberately no method here that
returns file content, a preview, or key material -- `admin-dashboard/spec.md`
requires that guarantee to hold by construction, not by the UI declining to
link to something.

Administrators are also not owners: the surface cannot grant them access to a
node, and cannot revoke a grant between two other users. That belongs to the
node's owner.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.cache import Dataset
from cyberfs.domain.errors import NotFoundError, PermissionDeniedError, ValidationError
from cyberfs.domain.ports.repositories import Page, UnitOfWork
from cyberfs.domain.sharing import PublicLink
from cyberfs.domain.stats import JobStatus, TenantStatistics, UserStorage
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: Placeholder shown instead of another user's file name when
#: `ADMIN_SHOW_FILENAMES` is off. Names are content-adjacent: "Q3 layoffs
#: final.xlsx" leaks the thing its owner wanted private.
REDACTED_NAME = "[redacted]"


@dataclass
class JobStatusRegistry:
    """Last-run state for the background jobs, for the operational view.

    In-process: a job that has not run on *this* replica reports as never run,
    which is honest rather than misleading. A durable record arrives with the
    scheduler.
    """

    statuses: dict[str, JobStatus] = field(default_factory=dict)

    def record(
        self,
        name: str,
        *,
        outcome: str,
        duration_seconds: float,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self.statuses[name] = JobStatus(
            name=name,
            last_run_at=now or utcnow(),
            outcome=outcome,
            duration_seconds=round(duration_seconds, 3),
            detail=detail,
        )

    def all(self, expected: tuple[str, ...]) -> tuple[JobStatus, ...]:
        """Every expected job, including ones that have never run."""
        return tuple(self.statuses.get(name, JobStatus(name=name)) for name in expected)

    def timer(self, name: str) -> _JobTimer:
        return _JobTimer(self, name)


class _JobTimer:
    """Records a job's outcome and duration whatever happens inside."""

    def __init__(self, registry: JobStatusRegistry, name: str) -> None:
        self._registry = registry
        self._name = name
        self._started = 0.0

    def __enter__(self) -> _JobTimer:
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: object, tb: object) -> None:
        self._registry.record(
            self._name,
            outcome="success" if exc_type is None else "failed",
            duration_seconds=time.perf_counter() - self._started,
            detail=None if exc_type is None else exc_type.__name__,
        )


EXPECTED_JOBS = ("purge", "orphan_reaper", "reconcile_quotas", "backup")


class AdminService:
    def __init__(
        self,
        *,
        show_filenames: bool = False,
        jobs: JobStatusRegistry | None = None,
    ) -> None:
        self._show_filenames = show_filenames
        self._jobs = jobs or JobStatusRegistry()

    @property
    def jobs(self) -> JobStatusRegistry:
        return self._jobs

    @property
    def show_filenames(self) -> bool:
        return self._show_filenames

    # --- statistics ----------------------------------------------------

    async def user_storage(self, uow: UnitOfWork, user_id: uuid.UUID) -> UserStorage:
        stats = await uow.admin.user_storage(user_id)
        if stats is None:
            raise NotFoundError("no such user", user_id=str(user_id))
        return stats

    async def list_users(
        self,
        uow: UnitOfWork,
        *,
        limit: int,
        sort_by: str = "used",
        over_quota_only: bool = False,
    ) -> tuple[UserStorage, ...]:
        users = await uow.admin.all_user_storage(limit=limit)
        if over_quota_only:
            users = tuple(u for u in users if u.over_quota)
        return tuple(sorted(users, key=_sort_key(sort_by), reverse=_descending(sort_by)))

    async def tenant(
        self, uow: UnitOfWork, *, growth_days: int = 30, top_n: int = 10
    ) -> TenantStatistics:
        if growth_days not in (7, 30, 90):
            raise ValidationError("growth window must be 7, 30, or 90 days")
        return await uow.admin.tenant(growth_days=growth_days, top_n=top_n)

    async def reconciles(self, uow: UnitOfWork) -> bool:
        """Whether reported totals agree with the sum of node sizes.

        The dashboard must not display drift indefinitely; this is what the
        reconciliation job's correctness is checked against.
        """
        tenant = await uow.admin.tenant()
        return tenant.live_bytes == await uow.admin.reconcile_total()

    # --- names ---------------------------------------------------------

    def present_name(self, name: str, *, owned_by_caller: bool) -> str:
        """Redact another user's file name unless explicitly enabled."""
        if owned_by_caller or self._show_filenames:
            return name
        return REDACTED_NAME

    async def note_filenames_enabled(self, uow: UnitOfWork, actor: str) -> None:
        """Enabling name disclosure is itself an auditable act."""
        if not self._show_filenames:
            return
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.FILENAMES_REVEALED,
                occurred_at=utcnow(),
                actor_subject=actor,
            )
        )

    # --- quota ---------------------------------------------------------

    async def set_quota(
        self,
        uow: UnitOfWork,
        actor: str,
        user_id: uuid.UUID,
        quota_bytes: int,
        *,
        now: datetime | None = None,
    ) -> UserStorage:
        """Change a user's quota.

        Lowering it below current usage is allowed: the user is simply marked
        over quota, keeps reading and deleting, and cannot upload. Refusing
        would leave an administrator unable to react to abuse.
        """
        moment = now or utcnow()
        if quota_bytes < 0:
            raise ValidationError("quota must not be negative")
        user = await uow.users.get(user_id)
        if user is None:
            raise NotFoundError("no such user", user_id=str(user_id))

        previous = user.quota_bytes
        user.quota_bytes = quota_bytes
        user.updated_at = moment
        await uow.users.update(user)
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.QUOTA_CHANGED,
                occurred_at=moment,
                actor_subject=actor,
                target_id=str(user_id),
                context={"previous_bytes": previous, "new_bytes": quota_bytes},
            )
        )
        await uow.flush()
        return await self.user_storage(uow, user_id)

    # --- sharing review ------------------------------------------------

    async def list_public_links(
        self, uow: UnitOfWork, *, limit: int, cursor: str | None = None
    ) -> Page[PublicLink]:
        return await uow.public_links.list_active(limit=limit, cursor=cursor)

    async def revoke_public_link(
        self,
        uow: UnitOfWork,
        actor: str,
        link_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> None:
        """Administrators may kill a public link -- it is a deployment-wide
        exposure, not a private arrangement between two users."""
        moment = now or utcnow()
        link = await uow.public_links.get(link_id)
        if link is None:
            raise NotFoundError("no such link")
        link.revoke(moment)
        await uow.public_links.update(link)
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.PUBLIC_LINK_REVOKED,
                occurred_at=moment,
                actor_subject=actor,
                target_id=str(link.node_id),
                context={"link_id": str(link_id), "by_admin": True},
            )
        )
        await uow.flush()

    @staticmethod
    def refuse_grant_revocation() -> None:
        """Grant management belongs to the node's owner.

        An administrator reaching into a private arrangement between two users
        is exactly the overreach `admin-dashboard/spec.md` forbids.
        """
        raise PermissionDeniedError(
            "grant management belongs to the node owner, not to administrators"
        )

    async def note_backup_triggered(self, uow: UnitOfWork, actor: str) -> None:
        """Record that an administrator started a backup by hand."""
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.BACKUP_TRIGGERED,
                occurred_at=utcnow(),
                actor_subject=actor,
            )
        )

    async def note_cache_purged(self, uow: UnitOfWork, actor: str, dataset: str) -> None:
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.CACHE_PURGED,
                occurred_at=utcnow(),
                actor_subject=actor,
                context={"dataset": dataset},
            )
        )

    # --- audit ---------------------------------------------------------

    async def audit(
        self,
        uow: UnitOfWork,
        *,
        actor_subject: str | None = None,
        action: str | None = None,
        target_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> Page[AuditRecord]:
        return await uow.audit.query(
            actor_subject=actor_subject,
            action=action,
            target_id=target_id,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
        )

    # --- operations ----------------------------------------------------

    def job_statuses(self) -> tuple[JobStatus, ...]:
        return self._jobs.all(EXPECTED_JOBS)

    @staticmethod
    def cacheable_datasets() -> tuple[str, ...]:
        return tuple(str(dataset) for dataset in Dataset)


#: Sortable columns for the user list. Everything is reduced to a single
#: comparable tuple so one code path handles them all.
_SORTS: dict[str, Callable[[UserStorage], tuple[float, str]]] = {
    "used": lambda u: (u.used_bytes, u.subject),
    "quota": lambda u: (u.percent_used, u.subject),
    "files": lambda u: (u.file_count, u.subject),
    "activity": lambda u: (
        u.last_seen_at.timestamp() if u.last_seen_at else 0.0,
        u.subject,
    ),
    "subject": lambda u: (0.0, u.subject),
}


def _sort_key(field_name: str) -> Callable[[UserStorage], tuple[float, str]]:
    return _SORTS.get(field_name, _SORTS["used"])


def _descending(field_name: str) -> bool:
    return field_name != "subject"
