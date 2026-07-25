"""Administrative endpoints.

Every route depends on `AdminPrincipal`, which is introspection-backed: an
administrator demoted a minute ago is denied on their next request rather than
when their token expires.

There is deliberately **no** route here that returns file content, a preview,
or key material. `tests/unit/test_admin_router.py` enumerates this router and
fails if one ever appears.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from cyberfs.adapters.inbound.api.dependencies import AdminPrincipal, UnitOfWorkDep
from cyberfs.adapters.inbound.api.health import readiness_components
from cyberfs.adapters.inbound.api.schemas import (
    AuditPage,
    BackupList,
    BackupRecordSummary,
    BackupSummary,
    JobSummary,
    LinkList,
    OperationsSummary,
    PurgeResponse,
    QuotaRequest,
    TenantSummary,
    UserStorageList,
    UserStorageSummary,
)
from cyberfs.application.admin import AdminService
from cyberfs.application.backup import BackupService
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.cache import Dataset
from cyberfs.domain.errors import ConflictError, NotFoundError, ValidationError
from cyberfs.infrastructure.scheduler import CronScheduler

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _service(request: Request) -> AdminService:
    service: AdminService = request.app.state.admin
    return service


# --- statistics ------------------------------------------------------------


@router.get("/overview", response_model=TenantSummary, summary="Deployment-wide statistics")
async def overview(
    request: Request,
    principal: AdminPrincipal,
    uow: UnitOfWorkDep,
    growth_days: Annotated[int, Query(ge=1, le=90)] = 30,
    top_n: Annotated[int, Query(ge=1, le=100)] = 10,
) -> TenantSummary:
    stats = await _service(request).tenant(uow, growth_days=growth_days, top_n=top_n)
    return TenantSummary.of(stats)


@router.get("/users", response_model=UserStorageList, summary="Per-user storage")
async def list_users(
    request: Request,
    principal: AdminPrincipal,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    sort_by: Annotated[str, Query()] = "used",
    over_quota: Annotated[bool, Query()] = False,
) -> UserStorageList:
    users = await _service(request).list_users(
        uow, limit=limit, sort_by=sort_by, over_quota_only=over_quota
    )
    return UserStorageList.of(users)


@router.get("/users/{user_id}", response_model=UserStorageSummary, summary="One user's storage")
async def user_detail(
    user_id: uuid.UUID, request: Request, principal: AdminPrincipal, uow: UnitOfWorkDep
) -> UserStorageSummary:
    return UserStorageSummary.of(await _service(request).user_storage(uow, user_id))


# --- quota -----------------------------------------------------------------


@router.put(
    "/users/{user_id}/quota", response_model=UserStorageSummary, summary="Set a user's quota"
)
async def set_quota(
    user_id: uuid.UUID,
    body: QuotaRequest,
    request: Request,
    principal: AdminPrincipal,
    uow: UnitOfWorkDep,
) -> UserStorageSummary:
    stats = await _service(request).set_quota(uow, principal.subject, user_id, body.quota_bytes)
    await uow.commit()
    return UserStorageSummary.of(stats)


# --- sharing review --------------------------------------------------------


@router.get("/links", response_model=LinkList, summary="Active public links")
async def list_links(
    request: Request,
    principal: AdminPrincipal,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> LinkList:
    page = await _service(request).list_public_links(uow, limit=limit, cursor=cursor)
    return LinkList.of(page.items)


@router.delete(
    "/links/{link_id}",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Revoke a public link",
)
async def revoke_link(
    link_id: uuid.UUID, request: Request, principal: AdminPrincipal, uow: UnitOfWorkDep
) -> Response:
    """A public link is a deployment-wide exposure, so an admin may kill it."""
    await _service(request).revoke_public_link(uow, principal.subject, link_id)
    await uow.commit()
    return Response(status_code=int(HTTPStatus.NO_CONTENT))


@router.delete(
    "/nodes/{node_id}/grants/{subject}",
    status_code=HTTPStatus.FORBIDDEN,
    summary="Refused: grants belong to the node owner",
)
async def refuse_grant_revocation(
    node_id: uuid.UUID, subject: str, request: Request, principal: AdminPrincipal
) -> Response:
    """Present so the refusal is explicit and discoverable, not a 404.

    An administrator reaching into a private arrangement between two users is
    the overreach `admin-dashboard/spec.md` forbids.
    """
    _service(request).refuse_grant_revocation()
    return Response(status_code=int(HTTPStatus.FORBIDDEN))  # unreachable


# --- audit -----------------------------------------------------------------


@router.get("/audit", response_model=AuditPage, summary="Browse the audit log")
async def audit(
    request: Request,
    principal: AdminPrincipal,
    uow: UnitOfWorkDep,
    actor: str | None = None,
    action: str | None = None,
    target: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> AuditPage:
    page = await _service(request).audit(
        uow,
        actor_subject=actor,
        action=action,
        target_id=target,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )
    return AuditPage.of(page)


# --- operations ------------------------------------------------------------


@router.get("/operations", response_model=OperationsSummary, summary="Health and jobs")
async def operations(
    request: Request, principal: AdminPrincipal, uow: UnitOfWorkDep
) -> OperationsSummary:
    service = _service(request)
    report = await request.app.state.health.readiness()
    return OperationsSummary(
        components=readiness_components(report),
        jobs=[JobSummary.of(status) for status in service.job_statuses()],
        cache=await request.app.state.cache.stats(),
        totals_reconcile=await service.reconciles(uow),
        backup=await _backup_summary(request),
    )


def _backup_service(request: Request) -> BackupService | None:
    service: BackupService | None = getattr(request.app.state, "backup", None)
    return service


async def _backup_summary(request: Request) -> BackupSummary:
    settings = request.app.state.settings
    backup = _backup_service(request)
    records = await backup.list_backups() if backup is not None else ()
    return BackupSummary.of(
        records,
        enabled=settings.backup_enabled,
        max_age_hours=settings.backup_max_age_hours,
        now=utcnow(),
    )


@router.post(
    "/operations/backup",
    response_model=BackupRecordSummary,
    summary="Trigger a backup by hand",
)
async def trigger_backup(
    request: Request, principal: AdminPrincipal, uow: UnitOfWorkDep
) -> BackupRecordSummary:
    """Run a backup now, with the same procedure and overlap guard as a
    scheduled one. Refused when backups are disabled, and 409 when a run is
    already in flight (`backup-restore/spec.md`, "Manual run")."""
    scheduler: CronScheduler | None = getattr(request.app.state, "backup_scheduler", None)
    job = getattr(request.app.state, "backup_job", None)
    if scheduler is None or job is None:
        raise ValidationError("backups are disabled")
    ran = await scheduler.trigger()
    if not ran:
        raise ConflictError("a backup is already running")
    await _service(request).note_backup_triggered(uow, principal.subject)
    await uow.commit()
    if job.last_record is None:  # pragma: no cover -- set by a completed run
        raise NotFoundError("backup produced no record")
    return BackupRecordSummary.of(job.last_record)


@router.get("/operations/backups", response_model=BackupList, summary="List backups")
async def list_backups(request: Request, principal: AdminPrincipal) -> BackupList:
    """History by timestamp, verification state, size, and schema revision."""
    backup = _backup_service(request)
    records = await backup.list_backups() if backup is not None else ()
    return BackupList.of(records)


@router.post(
    "/cache/{dataset}/purge", response_model=PurgeResponse, summary="Purge a cache dataset"
)
async def purge_cache(
    dataset: str, request: Request, principal: AdminPrincipal, uow: UnitOfWorkDep
) -> PurgeResponse:
    """Reports how many keys went, never what they held."""
    try:
        target = Dataset(dataset)
    except ValueError as exc:
        raise ValidationError(f"unknown cache dataset: {dataset}") from exc

    removed = await request.app.state.cache.purge(target)
    await _service(request).note_cache_purged(uow, principal.subject, dataset)
    await uow.commit()
    return PurgeResponse(dataset=dataset, keys_removed=removed)
