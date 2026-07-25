"""Sharing endpoints.

Grant changes use `FreshPrincipal`, not the cheap claim check: widening or
withdrawing access on the strength of a token whose holder was demoted a
minute ago is exactly the failure `authentication/spec.md` splits the two
modes to prevent.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import StreamingResponse

from cyberfs.adapters.inbound.api.dependencies import (
    CurrentUser,
    FreshPrincipal,
    UnitOfWorkDep,
)
from cyberfs.adapters.inbound.api.routers.content import stream_response
from cyberfs.adapters.inbound.api.schemas import (
    CreateLinkRequest,
    GrantList,
    GrantRequest,
    IssuedLinkResponse,
    LinkList,
    NodeDetail,
    NodeSummary,
    SearchResults,
    TransferRequest,
)
from cyberfs.application.content import ContentService
from cyberfs.application.sharing import SharingService
from cyberfs.domain.errors import NotFoundError
from cyberfs.domain.sharing import Role

router = APIRouter(prefix="/api/v1", tags=["sharing"])

Passphrase = Annotated[str | None, Header(alias="X-Link-Passphrase", max_length=255)]
RangeHeader = Annotated[str | None, Header(alias="Range")]


def _service(request: Request) -> SharingService:
    service: SharingService = request.app.state.sharing
    return service


def _source_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# --- grants ----------------------------------------------------------------


@router.put(
    "/nodes/{node_id}/grants",
    response_model=GrantList,
    status_code=HTTPStatus.CREATED,
    summary="Grant a role on a node",
)
async def create_grant(
    node_id: uuid.UUID,
    body: GrantRequest,
    request: Request,
    user: CurrentUser,
    principal: FreshPrincipal,
    uow: UnitOfWorkDep,
) -> GrantList:
    await _service(request).grant(uow, user, node_id, body.recipient, Role.parse(body.role))
    grants = await _service(request).list_grants(uow, user, node_id)
    await uow.commit()
    return GrantList.of(grants)


@router.get("/nodes/{node_id}/grants", response_model=GrantList, summary="List grants")
async def list_grants(
    node_id: uuid.UUID, request: Request, user: CurrentUser, uow: UnitOfWorkDep
) -> GrantList:
    return GrantList.of(await _service(request).list_grants(uow, user, node_id))


@router.delete(
    "/nodes/{node_id}/grants/{subject}",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Revoke a grant",
)
async def revoke_grant(
    node_id: uuid.UUID,
    subject: str,
    request: Request,
    user: CurrentUser,
    principal: FreshPrincipal,
    uow: UnitOfWorkDep,
) -> Response:
    await _service(request).revoke(uow, user, node_id, subject)
    await uow.commit()
    return Response(status_code=int(HTTPStatus.NO_CONTENT))


@router.get("/shared-with-me", response_model=SearchResults, summary="Shared with me")
async def shared_with_me(request: Request, user: CurrentUser, uow: UnitOfWorkDep) -> SearchResults:
    return SearchResults.of(await _service(request).shared_with_me(uow, user))


# --- public links ----------------------------------------------------------


@router.post(
    "/nodes/{node_id}/links",
    response_model=IssuedLinkResponse,
    status_code=HTTPStatus.CREATED,
    summary="Create a public link",
)
async def create_link(
    node_id: uuid.UUID,
    body: CreateLinkRequest,
    request: Request,
    user: CurrentUser,
    principal: FreshPrincipal,
    uow: UnitOfWorkDep,
) -> IssuedLinkResponse:
    issued = await _service(request).create_link(
        uow, user, node_id, expires_at=body.expires_at, passphrase=body.passphrase
    )
    await uow.commit()
    # The only time the token leaves the service.
    return IssuedLinkResponse.of_issued(issued.link, issued.token)


@router.get("/nodes/{node_id}/links", response_model=LinkList, summary="List public links")
async def list_links(
    node_id: uuid.UUID, request: Request, user: CurrentUser, uow: UnitOfWorkDep
) -> LinkList:
    return LinkList.of(await _service(request).list_links(uow, user, node_id))


@router.delete(
    "/links/{link_id}", status_code=HTTPStatus.NO_CONTENT, summary="Revoke a public link"
)
async def revoke_link(
    link_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    principal: FreshPrincipal,
    uow: UnitOfWorkDep,
) -> Response:
    await _service(request).revoke_link(uow, user, link_id)
    await uow.commit()
    return Response(status_code=int(HTTPStatus.NO_CONTENT))


# --- public (unauthenticated) link access ----------------------------------


@router.get("/public/{token}", response_model=NodeSummary, summary="Open a public link")
async def open_link(
    token: str,
    request: Request,
    uow: UnitOfWorkDep,
    passphrase: Passphrase = None,
) -> NodeSummary:
    """Deliberately unauthenticated: a link is the credential."""
    access = await _service(request).resolve_link(
        uow, token, passphrase=passphrase, source_ip=_source_ip(request)
    )
    await uow.commit()
    return NodeSummary.of(access.node)


@router.get("/public/{token}/content", summary="Download through a public link")
async def download_via_link(
    token: str,
    request: Request,
    uow: UnitOfWorkDep,
    passphrase: Passphrase = None,
    range_header: RangeHeader = None,
) -> StreamingResponse:
    sharing = _service(request)
    access = await sharing.resolve_link(
        uow, token, passphrase=passphrase, source_ip=_source_ip(request)
    )
    content: ContentService = request.app.state.content
    owner = await uow.users.get(access.node.owner_id)
    if owner is None:
        raise NotFoundError("no such link")
    # Read as the owner: the link carries the owner's read access, never more.
    # The read is attributed to the link, not the owner: there is no
    # authenticated caller, and it is not the owner's own activity.
    plan = await content.download(
        uow, owner, access.node.id, range_header=range_header, link_id=access.link.id
    )
    await uow.commit()
    return stream_response(plan)


# --- ownership -------------------------------------------------------------


@router.post("/nodes/{node_id}/owner", response_model=NodeDetail, summary="Transfer ownership")
async def transfer_ownership(
    node_id: uuid.UUID,
    body: TransferRequest,
    request: Request,
    user: CurrentUser,
    principal: FreshPrincipal,
    uow: UnitOfWorkDep,
    response: Response,
) -> NodeDetail:
    await _service(request).transfer_ownership(
        uow, user, node_id, body.recipient, keep_editor_access=body.keep_editor_access
    )
    view = await request.app.state.nodes.get(uow, user, node_id)
    await uow.commit()
    response.headers["ETag"] = view.etag
    return NodeDetail.of_view(view)
