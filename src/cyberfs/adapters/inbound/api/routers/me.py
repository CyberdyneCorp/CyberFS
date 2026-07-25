"""The caller's own surface.

`GET /api/v1/me/activity` returns only the calling user's operations. It is
self-scoped by construction: there is no path or query parameter naming another
subject, so a caller cannot ask for anyone else's activity. Service principals
own no tree and are refused.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from cyberfs.adapters.inbound.api.dependencies import CurrentPrincipal, UnitOfWorkDep
from cyberfs.adapters.inbound.api.schemas import ActivityResponse
from cyberfs.application.activity import ActivityService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.errors import PermissionDeniedError

router = APIRouter(prefix="/api/v1/me", tags=["me"])


def _service(request: Request) -> ActivityService:
    service: ActivityService = request.app.state.activity
    return service


@router.get("/activity", response_model=ActivityResponse, summary="Your own activity")
async def activity(
    request: Request,
    principal: CurrentPrincipal,
    uow: UnitOfWorkDep,
    window_days: Annotated[int, Query(ge=1)] = 30,
    action: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    cursor: str | None = None,
) -> ActivityResponse:
    """A rollup plus a newest-first, paginated feed of the caller's operations.

    Refuses a service principal (403), and a window beyond the configured
    maximum (422). The subject is always the caller's own -- there is no
    parameter by which another user's activity could be requested.
    """
    if principal.is_service:
        raise PermissionDeniedError("a service principal has no activity of its own")

    provisioning: ProvisioningService = request.app.state.provisioning
    user = await provisioning.resolve(uow, principal)
    await uow.commit()

    rollup, feed = await _service(request).activity(
        uow,
        user,
        window_days=window_days,
        action=action,
        limit=limit,
        cursor=cursor,
    )
    return ActivityResponse.of(rollup, feed)
