"""Filesystem endpoints.

Thin: parse, delegate to the use case, serialize. Authorization, invariants,
and transaction boundaries all live inward of here.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response

from cyberfs.adapters.inbound.api.dependencies import CurrentUser, UnitOfWorkDep
from cyberfs.adapters.inbound.api.schemas import (
    CopyRequest,
    CreateFolderRequest,
    DeleteResult,
    MoveRequest,
    NodeDetail,
    NodePage,
    PurgeResult,
    RenameRequest,
    SearchResults,
)
from cyberfs.application.nodes import NodeService

router = APIRouter(prefix="/api/v1", tags=["nodes"])

IfMatch = Annotated[str | None, Header(alias="If-Match")]


def _service(request: Request) -> NodeService:
    service: NodeService = request.app.state.nodes
    return service


def _with_etag(response: Response, detail: NodeDetail) -> NodeDetail:
    """Publish the ETag so a client can send it back as `If-Match`."""
    response.headers["ETag"] = detail.etag
    return detail


@router.get("/nodes/root", response_model=NodeDetail, summary="The caller's root folder")
async def get_root(
    request: Request, user: CurrentUser, uow: UnitOfWorkDep, response: Response
) -> NodeDetail:
    view = await _service(request).get(uow, user, user.root_folder_id)
    return _with_etag(response, NodeDetail.of_view(view))


@router.get("/nodes/{node_id}", response_model=NodeDetail, summary="Node metadata")
async def get_node(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
) -> NodeDetail:
    view = await _service(request).get(uow, user, node_id)
    return _with_etag(response, NodeDetail.of_view(view))


@router.get(
    "/nodes/{node_id}/children", response_model=NodePage, summary="List a folder's children"
)
async def list_children(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> NodePage:
    page = await _service(request).list_children(uow, user, node_id, limit=limit, cursor=cursor)
    return NodePage.of(page)


@router.post(
    "/nodes/{node_id}/folders",
    response_model=NodeDetail,
    status_code=HTTPStatus.CREATED,
    summary="Create a folder",
)
async def create_folder(
    node_id: uuid.UUID,
    body: CreateFolderRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
) -> NodeDetail:
    view = await _service(request).create_folder(
        uow, user, node_id, body.name, encryption_default=body.encryption_default
    )
    await uow.commit()
    return _with_etag(response, NodeDetail.of_view(view))


@router.patch("/nodes/{node_id}/name", response_model=NodeDetail, summary="Rename a node")
async def rename_node(
    node_id: uuid.UUID,
    body: RenameRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
    if_match: IfMatch = None,
) -> NodeDetail:
    view = await _service(request).rename(uow, user, node_id, body.name, if_match=if_match)
    await uow.commit()
    return _with_etag(response, NodeDetail.of_view(view))


@router.patch("/nodes/{node_id}/parent", response_model=NodeDetail, summary="Move a node")
async def move_node(
    node_id: uuid.UUID,
    body: MoveRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
    if_match: IfMatch = None,
) -> NodeDetail:
    view = await _service(request).move(uow, user, node_id, body.parent_id, if_match=if_match)
    await uow.commit()
    return _with_etag(response, NodeDetail.of_view(view))


@router.post(
    "/nodes/{node_id}/copy",
    response_model=NodeDetail,
    status_code=HTTPStatus.CREATED,
    summary="Copy a node into another folder",
)
async def copy_node(
    node_id: uuid.UUID,
    body: CopyRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
) -> NodeDetail:
    view = await _service(request).copy(
        uow,
        user,
        node_id,
        body.parent_id,
        name=body.name,
        # The content service duplicates objects server-side, so a copy never
        # transits the API and encrypted content is copied as ciphertext.
        content=request.app.state.content,
    )
    await uow.commit()
    return _with_etag(response, NodeDetail.of_view(view))


@router.delete("/nodes/{node_id}", response_model=DeleteResult, summary="Move to trash")
async def delete_node(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    if_match: IfMatch = None,
) -> DeleteResult:
    deleted = await _service(request).delete(uow, user, node_id, if_match=if_match)
    await uow.commit()
    return DeleteResult(deleted=deleted)


@router.post(
    "/nodes/{node_id}/purge",
    response_model=PurgeResult,
    summary="Destroy a trashed node permanently",
    responses={
        int(HTTPStatus.CONFLICT): {"description": "The node is not in the trash"},
        int(HTTPStatus.NOT_FOUND): {"description": "No such node, or already purged"},
    },
)
async def purge_node(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
) -> PurgeResult:
    """Irreversible. The node must already have been moved to the trash."""
    purged = await _service(request).purge(
        uow,
        user,
        node_id,
        # The object store is the only way to actually free the bytes; metadata
        # deletion alone would strand them.
        objects=request.app.state.objects,
    )
    await uow.commit()
    return PurgeResult(
        purged=purged.nodes_deleted,
        objects_deleted=purged.objects_deleted,
        bytes_reclaimed=purged.bytes_reclaimed,
    )


@router.post("/nodes/{node_id}/restore", response_model=NodeDetail, summary="Restore from trash")
async def restore_node(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
) -> NodeDetail:
    view = await _service(request).restore(uow, user, node_id)
    await uow.commit()
    return _with_etag(response, NodeDetail.of_view(view))


@router.get("/search", response_model=SearchResults, summary="Search node metadata")
async def search(
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    q: Annotated[str, Query(min_length=1, max_length=255)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> SearchResults:
    return SearchResults.of(await _service(request).search(uow, user, q, limit=limit))
