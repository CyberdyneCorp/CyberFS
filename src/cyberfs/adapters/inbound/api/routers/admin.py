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
from cyberfs.domain.cache import Dataset
from cyberfs.domain.errors import ValidationError

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
    )


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
