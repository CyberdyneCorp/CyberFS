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
    MetadataPatchRequest,
    MetadataRequest,
    MoveRequest,
    NodeDetail,
    NodePage,
    PurgeResult,
    RenameRequest,
    TagPage,
    TagPatchRequest,
    TagsRequest,
)
from cyberfs.application.nodes import NodeService, NodeView
from cyberfs.domain.nodes import MAX_TAG_LENGTH
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.search import TagMatch

router = APIRouter(prefix="/api/v1", tags=["nodes"])

IfMatch = Annotated[str | None, Header(alias="If-Match")]


def _service(request: Request) -> NodeService:
    service: NodeService = request.app.state.nodes
    return service


def _with_etag(response: Response, detail: NodeDetail) -> NodeDetail:
    """Publish the ETag so a client can send it back as `If-Match`."""
    response.headers["ETag"] = detail.etag
    return detail


async def _detail(request: Request, uow: UnitOfWork, view: NodeView) -> NodeDetail:
    """A full node response, carrying its labels and its content digest.

    Every route that returns a `NodeDetail` goes through here, so a caller never
    has to guess whether a given endpoint populates them.
    """
    service = _service(request)
    tags, metadata = await service.labels_for(uow, view.node.id)
    digest = await service.current_digest(uow, view.node)
    return NodeDetail.of_view(view, tags=tags, metadata=metadata, digest=digest)


@router.get("/nodes/root", response_model=NodeDetail, summary="The caller's root folder")
async def get_root(
    request: Request, user: CurrentUser, uow: UnitOfWorkDep, response: Response
) -> NodeDetail:
    view = await _service(request).get(uow, user, user.root_folder_id)
    return _with_etag(response, await _detail(request, uow, view))


@router.get("/nodes/{node_id}", response_model=NodeDetail, summary="Node metadata")
async def get_node(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
) -> NodeDetail:
    view = await _service(request).get(uow, user, node_id)
    return _with_etag(response, await _detail(request, uow, view))


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
    return _with_etag(response, await _detail(request, uow, view))


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
    return _with_etag(response, await _detail(request, uow, view))


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
    return _with_etag(response, await _detail(request, uow, view))


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
    return _with_etag(response, await _detail(request, uow, view))


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


@router.put(
    "/nodes/{node_id}/tags",
    response_model=NodeDetail,
    summary="Replace a node's tags",
)
async def replace_tags(
    node_id: uuid.UUID,
    body: TagsRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
    if_match: IfMatch = None,
) -> NodeDetail:
    """Replaces the whole set; an empty list clears it.

    Use `PATCH` to add or remove individual tags without restating the rest.
    """
    view, _ = await _service(request).replace_tags(uow, user, node_id, body.tags, if_match=if_match)
    await uow.commit()
    return _with_etag(response, await _detail(request, uow, view))


@router.put(
    "/nodes/{node_id}/metadata",
    response_model=NodeDetail,
    summary="Replace a node's key/value metadata",
)
async def replace_metadata(
    node_id: uuid.UUID,
    body: MetadataRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
    if_match: IfMatch = None,
) -> NodeDetail:
    """Replaces every pair a caller may write; an empty list clears them.

    Pairs in the `cyberfs.` namespace reserved for system use are outside that:
    they survive the replace and do not appear in the response, so what comes back
    is exactly what may be written again. Use `PATCH` to set or delete individual
    keys.
    """
    view, _ = await _service(request).replace_metadata(
        uow,
        user,
        node_id,
        [(pair.key, pair.value) for pair in body.metadata],
        if_match=if_match,
    )
    await uow.commit()
    return _with_etag(response, await _detail(request, uow, view))


@router.patch(
    "/nodes/{node_id}/tags",
    response_model=NodeDetail,
    summary="Add and remove individual tags",
)
async def patch_tags(
    node_id: uuid.UUID,
    body: TagPatchRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
    if_match: IfMatch = None,
) -> NodeDetail:
    """Merges: tags the request does not name are left alone.

    A delta that turns out to change nothing is a success that writes nothing --
    same body, same ETag. Naming the same tag in both directions is refused.
    """
    view, _ = await _service(request).patch_tags(
        uow, user, node_id, add=body.add, remove=body.remove, if_match=if_match
    )
    await uow.commit()
    return _with_etag(response, await _detail(request, uow, view))


@router.patch(
    "/nodes/{node_id}/metadata",
    response_model=NodeDetail,
    summary="Set and delete individual metadata keys",
)
async def patch_metadata(
    node_id: uuid.UUID,
    body: MetadataPatchRequest,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    response: Response,
    if_match: IfMatch = None,
) -> NodeDetail:
    """Merges: keys the request does not name keep their values byte for byte."""
    view, _ = await _service(request).patch_metadata(
        uow,
        user,
        node_id,
        pairs=[(pair.key, pair.value) for pair in body.set],
        remove=body.remove,
        if_match=if_match,
    )
    await uow.commit()
    return _with_etag(response, await _detail(request, uow, view))


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
    return _with_etag(response, await _detail(request, uow, view))


@router.get("/search", response_model=NodePage, summary="Search node metadata")
async def search(
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    q: Annotated[str | None, Query(max_length=255)] = None,
    tag: Annotated[list[str] | None, Query(max_length=MAX_TAG_LENGTH)] = None,
    key: Annotated[str | None, Query(max_length=128)] = None,
    value: Annotated[str | None, Query(max_length=1024)] = None,
    tag_match: TagMatch = TagMatch.ALL,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> NodePage:
    """Name, tags, and metadata. Content is never indexed, so never matched.

    Filters narrow: repeating `tag` requires all of them unless `tag_match=any`,
    and `value` pins the `key` it accompanies. At least one filter is required.

    Cursor-paginated in name order, ties broken by identifier. A cursor belongs
    to the filters it was issued for; presenting it with others is refused.
    """
    page = await _service(request).search(
        uow,
        user,
        q,
        tags=tag or (),
        key=key,
        value=value,
        tag_match=tag_match,
        limit=limit,
        cursor=cursor,
    )
    return NodePage.of(page)


@router.get("/tags", response_model=TagPage, summary="The caller's tags, with usage counts")
async def list_tags(
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    prefix: Annotated[str | None, Query(max_length=MAX_TAG_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> TagPage:
    """Every tag in use across the nodes the caller may search, tag order.

    The counts are that caller's: they cover the nodes they own or hold an
    active grant on, so they are not a property of the tag. `prefix` is matched
    against the normalized tag form, for type-ahead.
    """
    page = await _service(request).tag_inventory(
        uow, user, prefix=prefix, limit=limit, cursor=cursor
    )
    return TagPage.of(page)
