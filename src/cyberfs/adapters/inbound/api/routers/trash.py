"""The caller's trash.

A top-level collection, like `/api/v1/search`: the trash is per-user and spans
the whole tree, so hanging it off a folder would misdescribe what it holds. It is
self-scoped by construction -- there is no path or query parameter naming another
subject, and no counterpart under `/api/v1/admin/*`, where node names are gated
behind `ADMIN_SHOW_FILENAMES` and a trash listing would be an ungated channel for
exactly the thing that gate exists to withhold.

Destruction is `POST /trash/purge`, not `DELETE /trash`: `DELETE` in this API
means the recoverable soft delete everywhere it appears, and reusing it for
permanent destruction of a whole trash would make irreversibility a property of a
recoverable verb at the largest radius available.

Both routes reach `NodeService`, beside the delete and restore they complete.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query, Request

from cyberfs.adapters.inbound.api.dependencies import CurrentUser, UnitOfWorkDep
from cyberfs.adapters.inbound.api.schemas import (
    EmptyTrashRequest,
    EmptyTrashResult,
    TrashPage,
)
from cyberfs.application.nodes import NodeService

router = APIRouter(prefix="/api/v1/trash", tags=["trash"])


def _service(request: Request) -> NodeService:
    service: NodeService = request.app.state.nodes
    return service


@router.get("", response_model=TrashPage, summary="Your own trash")
async def list_trash(
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> TrashPage:
    """One entry per deletion, most recently deleted first.

    Each entry carries the path it came from, when it goes, and what restoring it
    would bring back, so choosing between restore and purge needs no further
    request -- a trashed node is deliberately not readable individually.

    `total_entries` is the whole trash, not this page: it is the number
    `POST /trash/purge` requires, and deriving it by paginating the trash would
    make that guard unsatisfiable on a first call.
    """
    listing = await _service(request).trash(uow, user, limit=limit, cursor=cursor)
    return TrashPage.of(listing)


@router.post(
    "/purge",
    response_model=EmptyTrashResult,
    summary="Destroy every entry in your trash",
    responses={
        int(HTTPStatus.CONFLICT): {
            "description": "`trash_count_mismatch`: the trash does not hold the stated "
            "number of entries, and nothing was destroyed. List it again and retry with "
            "the total the listing reports."
        },
    },
)
async def empty_trash(
    body: EmptyTrashRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
) -> EmptyTrashResult:
    """Irreversible, and bounded by `TRASH_PURGE_NODE_BUDGET` nodes per call.

    Nodes, not entries: an entry is the root of a subtree of unbounded size, so a
    bound on entries would bound nothing. `entries_remaining` in the response is
    what is left, so a client loops until it reaches zero, restating that number
    each time. A stale count is refused with `409 trash_count_mismatch`, which is
    what makes a blind retry fail loudly rather than destroy whatever has since
    been trashed.
    """
    emptied = await _service(request).empty_trash(
        uow,
        user,
        expected_entries=body.expected_entries,
        # Only the object store can actually free the bytes; deleting metadata
        # alone would strand them.
        objects=request.app.state.objects,
    )
    await uow.commit()
    return EmptyTrashResult.of(emptied)
