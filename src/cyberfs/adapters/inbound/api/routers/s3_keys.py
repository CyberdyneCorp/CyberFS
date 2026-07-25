"""S3 access-key management, under the caller's own surface.

`POST`/`GET`/`DELETE` at `/api/v1/me/s3-keys`. The caller can only ever act on
their own keys: the owning subject is taken from the authenticated principal,
never from a path or body parameter, so no request can name another user's
credentials. Service principals own no tree and are refused.

The secret is returned exactly once, from the mint route, and never appears in
any listing thereafter -- see `authentication/spec.md`, "Key material is stored
irreversibly".
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request, Response

from cyberfs.adapters.inbound.api.dependencies import CurrentPrincipal, UnitOfWorkDep
from cyberfs.adapters.inbound.api.schemas import (
    CreateS3KeyRequest,
    IssuedS3KeyResponse,
    S3KeyList,
)
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.s3_keys import S3AccessKeyService

router = APIRouter(prefix="/api/v1/me/s3-keys", tags=["s3-keys"])


def _service(request: Request) -> S3AccessKeyService:
    service: S3AccessKeyService = request.app.state.s3_keys
    return service


@router.post(
    "",
    response_model=IssuedS3KeyResponse,
    status_code=HTTPStatus.CREATED,
    summary="Mint an S3 access key",
)
async def create_key(
    request: Request,
    principal: CurrentPrincipal,
    uow: UnitOfWorkDep,
    body: CreateS3KeyRequest,
) -> IssuedS3KeyResponse:
    """Create a key and return its secret once. Refused to service principals."""
    provisioning: ProvisioningService = request.app.state.provisioning
    user = await provisioning.resolve(uow, principal)
    issued = await _service(request).mint(uow, principal, user, label=body.label)
    await uow.commit()
    return IssuedS3KeyResponse.of_issued(issued.key, issued.secret)


@router.get("", response_model=S3KeyList, summary="List your S3 access keys")
async def list_keys(
    request: Request,
    principal: CurrentPrincipal,
    uow: UnitOfWorkDep,
) -> S3KeyList:
    """Every key the caller owns. The secret is never included."""
    keys = await _service(request).list(uow, principal.subject)
    return S3KeyList.of(keys)


@router.delete(
    "/{key_id}",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Revoke an S3 access key",
)
async def revoke_key(
    request: Request,
    principal: CurrentPrincipal,
    uow: UnitOfWorkDep,
    key_id: str,
) -> Response:
    """Revoke immediately. A key belonging to another subject is 404."""
    await _service(request).revoke(uow, principal.subject, key_id)
    await uow.commit()
    return Response(status_code=int(HTTPStatus.NO_CONTENT))
