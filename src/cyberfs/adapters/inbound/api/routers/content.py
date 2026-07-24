"""Upload and download.

Bytes stream in both directions. `file-storage/spec.md` forbids issuing a
presigned URL, so every byte passes through here and every byte is therefore
subject to authorization, quota, and (later) encryption.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from cyberfs.adapters.inbound.api.dependencies import CurrentUser, UnitOfWorkDep
from cyberfs.adapters.inbound.api.schemas import NodeSummary, VersionList
from cyberfs.application.content import ContentService, DownloadPlan

router = APIRouter(prefix="/api/v1", tags=["content"])

IfMatch = Annotated[str | None, Header(alias="If-Match")]
RangeHeader = Annotated[str | None, Header(alias="Range")]
ContentLength = Annotated[int | None, Header(alias="Content-Length")]
ContentType = Annotated[str | None, Header(alias="Content-Type")]


def _service(request: Request) -> ContentService:
    service: ContentService = request.app.state.content
    return service


def stream_response(plan: DownloadPlan) -> StreamingResponse:
    headers = {
        # The *plaintext* length: what the caller receives, which is not the
        # stored object's size once encryption is in play.
        "Content-Length": str(plan.served_bytes),
        "Accept-Ranges": "bytes",
        "ETag": plan.node.etag,
        # Names can be content-adjacent, so they are not echoed into headers
        # that proxies and access logs routinely capture.
        "Content-Disposition": "attachment",
    }
    status = HTTPStatus.OK
    if plan.content_range is not None:
        span = plan.content_range
        headers["Content-Range"] = f"bytes {span.start}-{span.end}/{plan.total_bytes}"
        status = HTTPStatus.PARTIAL_CONTENT
    return StreamingResponse(
        plan.stream,
        status_code=int(status),
        media_type=plan.version.content_type,
        headers=headers,
    )


@router.put(
    "/nodes/{node_id}/content",
    response_model=NodeSummary,
    summary="Replace a file's content, creating a new version",
)
async def replace_content(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    content_type: ContentType = None,
    content_length: ContentLength = None,
) -> NodeSummary:
    node = await _service(request).replace(
        uow,
        user,
        node_id,
        request.stream(),
        content_type=content_type,
        declared_length=content_length,
    )
    await uow.commit()
    return NodeSummary.of(node)


@router.put(
    "/nodes/{node_id}/files/{name}",
    response_model=NodeSummary,
    status_code=HTTPStatus.CREATED,
    summary="Upload a file into a folder",
)
async def upload_file(
    node_id: uuid.UUID,
    name: str,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    content_type: ContentType = None,
    content_length: ContentLength = None,
) -> NodeSummary:
    node = await _service(request).upload(
        uow,
        user,
        node_id,
        name,
        request.stream(),
        content_type=content_type,
        declared_length=content_length,
    )
    await uow.commit()
    return NodeSummary.of(node)


@router.get("/nodes/{node_id}/content", summary="Download a file")
async def download_content(
    node_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
    range_header: RangeHeader = None,
    version_id: Annotated[uuid.UUID | None, Query(alias="version")] = None,
) -> StreamingResponse:
    plan = await _service(request).download(
        uow, user, node_id, range_header=range_header, version_id=version_id
    )
    return stream_response(plan)


@router.get(
    "/nodes/{node_id}/versions", response_model=VersionList, summary="List content versions"
)
async def list_versions(
    node_id: uuid.UUID, request: Request, user: CurrentUser, uow: UnitOfWorkDep
) -> VersionList:
    return VersionList.of(await _service(request).list_versions(uow, user, node_id))


@router.post(
    "/nodes/{node_id}/versions/{version_id}/restore",
    response_model=NodeSummary,
    summary="Restore an earlier version as a new one",
)
async def restore_version(
    node_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    uow: UnitOfWorkDep,
) -> NodeSummary:
    node = await _service(request).restore_version(uow, user, node_id, version_id)
    await uow.commit()
    return NodeSummary.of(node)
