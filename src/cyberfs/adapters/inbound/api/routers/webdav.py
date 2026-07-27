"""WebDAV Class 1 surface.

Built like `routers/s3.py`: one router factory rooted at a configurable base
path, mounted only when enabled, every failure rendered in the protocol's own
error format rather than the REST problem document.

Every method delegates to the use case its REST equivalent calls. That is the
property worth protecting: quota, encryption inheritance, the trash, auditing and
the activity feed cannot drift between surfaces, because there is only one
implementation of each. A WebDAV layer that reimplemented any of them would be a
second place for a rule to be wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request, Response

from cyberfs.adapters.inbound.api.dependencies import UnitOfWorkDep
from cyberfs.adapters.inbound.api.routers.content import stream_response
from cyberfs.application.content import ContentService
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.s3_authentication import principal_from_key
from cyberfs.application.webdav_auth import WebDavAuthenticator, WebDavAuthError
from cyberfs.domain import webdav
from cyberfs.domain.errors import (
    CyberFSError,
    NameTakenError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    ValidationError,
)
from cyberfs.domain.nodes import Node, normalize_name
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.users import User
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

XML_MEDIA_TYPE = 'application/xml; charset="utf-8"'
_MULTI_STATUS = 207
_INSUFFICIENT_STORAGE = 507


def build_webdav_router(*, base_path: str, requires_tls: bool) -> APIRouter:
    """The WebDAV router rooted at `base_path`.

    `requires_tls` refuses a plaintext request. With the surface mounted by
    default this is the guard that matters: Basic authentication carries the
    secret on every request, so a deployment that never opted in must not be able
    to leak one because TLS terminated somewhere unexpected.
    """
    router = APIRouter(tags=["webdav"])
    # LOCK/UNLOCK/PROPPATCH are routed so they can be refused with 405 rather
    # than 404, which tells a client the surface exists but will not do that.
    methods = [*webdav.ALLOWED_METHODS, "LOCK", "UNLOCK", "PROPPATCH"]

    @router.api_route(base_path, methods=methods, include_in_schema=False)
    @router.api_route(f"{base_path}/{{path:path}}", methods=methods, include_in_schema=False)
    async def dispatch(request: Request, uow: UnitOfWorkDep, path: str = "") -> Response:
        try:
            _ensure_secure(request, requires_tls)
            if request.method in ("LOCK", "UNLOCK", "PROPPATCH"):
                # Refused rather than faked. CyberFS has no lock concept and its
                # concurrency control is optimistic `If-Match`; a lock that does
                # not stop a concurrent REST write would be a lie.
                return _refuse(HTTPStatus.METHOD_NOT_ALLOWED, f"{request.method} is not supported")
            if request.method == "OPTIONS":
                # Answered before authenticating: it discloses nothing about
                # content, only which methods exist.
                return _options()
            user = await _authenticate(request, uow)
            return await _handle(request, uow, user, path, base_path)
        except WebDavAuthError:
            return _challenge()
        except CyberFSError as exc:
            return _refuse(*_status_for(exc))

    return router


# --- dispatch ---------------------------------------------------------------


async def _handle(
    request: Request, uow: UnitOfWork, user: User, path: str, base_path: str
) -> Response:
    method = request.method
    if method == "PROPFIND":
        return await _propfind(request, uow, user, path, base_path)
    if method in ("GET", "HEAD"):
        return await _read(request, uow, user, path)
    if method == "PUT":
        return await _put(request, uow, user, path)
    if method == "DELETE":
        return await _delete(request, uow, user, path)
    if method == "MKCOL":
        return await _mkcol(request, uow, user, path)
    if method in ("COPY", "MOVE"):
        return await _copy_or_move(request, uow, user, path, base_path)
    return _refuse(HTTPStatus.METHOD_NOT_ALLOWED, f"{method} is not supported")


async def _propfind(
    request: Request, uow: UnitOfWork, user: User, path: str, base_path: str
) -> Response:
    depth = request.headers.get("depth", "1")
    if depth not in webdav.SUPPORTED_DEPTHS:
        # `infinity` is a recursive walk of an unbounded subtree in one request.
        return _refuse(HTTPStatus.FORBIDDEN, "only Depth 0 and 1 are supported")

    node = await _resolve(uow, user, path)
    entries: list[tuple[Node, str]] = [(node, path)]
    if depth == "1" and node.is_folder:
        page = await _nodes(request).list_children(uow, user, node.id, limit=_page_limit(request))
        entries += [(child, f"{path}/{child.name}".lstrip("/")) for child in page.items]

    return Response(
        content=webdav.multistatus(base_path, entries),
        status_code=_MULTI_STATUS,
        media_type=XML_MEDIA_TYPE,
    )


async def _read(request: Request, uow: UnitOfWork, user: User, path: str) -> Response:
    node = await _resolve(uow, user, path)
    if node.is_folder:
        # A collection has no body to serve. Clients probe this.
        return _refuse(HTTPStatus.METHOD_NOT_ALLOWED, "a collection has no content")
    if request.method == "HEAD":
        return Response(
            status_code=HTTPStatus.OK,
            headers={
                "Content-Length": str(node.size_bytes),
                "Content-Type": node.content_type or "application/octet-stream",
                "ETag": node.etag,
                "Accept-Ranges": "bytes",
            },
        )
    plan = await _content(request).download(
        uow, user, node.id, range_header=request.headers.get("range")
    )
    # The read emits a download audit record; commit it before streaming.
    await uow.commit()
    return stream_response(plan)


async def _put(request: Request, uow: UnitOfWork, user: User, path: str) -> Response:
    parent_path, name = _split(path)
    if not name:
        return _refuse(HTTPStatus.METHOD_NOT_ALLOWED, "a collection cannot be written to")
    parent = await _resolve(uow, user, parent_path)
    body = await request.body()
    existing = await uow.nodes.get_child_by_name(parent.id, normalize_name(name))

    async def stream() -> AsyncIterator[bytes]:
        yield body

    if existing is not None and not existing.is_deleted:
        # An overwrite is a new version, exactly as a REST replace would be.
        await _content(request).replace(
            uow, user, existing.id, stream(), content_type=_content_type(request)
        )
        status = HTTPStatus.NO_CONTENT
    else:
        await _content(request).upload(
            uow, user, parent.id, name, stream(), content_type=_content_type(request)
        )
        status = HTTPStatus.CREATED
    await uow.commit()
    return Response(status_code=status)


async def _delete(request: Request, uow: UnitOfWork, user: User, path: str) -> Response:
    node = await _resolve(uow, user, path)
    # Soft delete: WebDAV must not be a way around the trash.
    await _nodes(request).delete(uow, user, node.id)
    await uow.commit()
    return Response(status_code=HTTPStatus.NO_CONTENT)


async def _mkcol(request: Request, uow: UnitOfWork, user: User, path: str) -> Response:
    parent_path, name = _split(path)
    if not name:
        return _refuse(HTTPStatus.METHOD_NOT_ALLOWED, "the root already exists")
    parent = await _resolve(uow, user, parent_path)
    try:
        await _nodes(request).create_folder(uow, user, parent.id, name)
    except NameTakenError:
        # RFC 4918 9.3.1: MKCOL "can only be executed on an unmapped URL", and
        # names the status for a mapped one as 405 -- not the 412 every other
        # taken-name refusal on this surface returns. The distinction is not
        # pedantry: a client syncing a tree calls MKCOL on directories that may
        # already exist and treats 405 as "already there, carry on", where 412
        # reads as a precondition it never set and aborts the sync.
        return _refuse(HTTPStatus.METHOD_NOT_ALLOWED, "the collection already exists")
    await uow.commit()
    return Response(status_code=HTTPStatus.CREATED)


async def _copy_or_move(
    request: Request, uow: UnitOfWork, user: User, path: str, base_path: str
) -> Response:
    destination = request.headers.get("destination")
    if not destination:
        return _refuse(HTTPStatus.BAD_REQUEST, "Destination is required")
    target = _destination_path(destination, base_path)
    if target is None:
        return _refuse(HTTPStatus.BAD_REQUEST, "Destination is outside this surface")

    source = await _resolve(uow, user, path)
    target_parent_path, target_name = _split(target)
    target_parent = await _resolve(uow, user, target_parent_path)

    overwrite = request.headers.get("overwrite", "T").upper() != "F"
    clash = await uow.nodes.get_child_by_name(target_parent.id, normalize_name(target_name))
    if clash is not None and not clash.is_deleted:
        if not overwrite:
            return _refuse(HTTPStatus.PRECONDITION_FAILED, "destination exists")
        # Overwrite is expressed as trashing the occupant first, so the name is
        # free and the displaced node stays recoverable.
        await _nodes(request).delete(uow, user, clash.id)

    service = _nodes(request)
    if request.method == "COPY":
        await service.copy(
            uow, user, source.id, target_parent.id, name=target_name, content=_content(request)
        )
    else:
        if target_parent.id != source.parent_id:
            await service.move(uow, user, source.id, target_parent.id)
        if normalize_name(target_name) != source.normalized_name:
            await service.rename(uow, user, source.id, target_name)
    await uow.commit()
    return Response(status_code=HTTPStatus.CREATED)


# --- helpers ----------------------------------------------------------------


def _options() -> Response:
    return Response(
        status_code=HTTPStatus.OK,
        headers={
            "DAV": webdav.DAV_COMPLIANCE,
            "Allow": ", ".join(webdav.ALLOWED_METHODS),
            "MS-Author-Via": "DAV",
        },
    )


def _challenge() -> Response:
    """`401` with the challenge, so a client knows what to offer.

    Identical for absent, unknown, wrong and revoked credentials: a client learns
    that it failed, never which way.
    """
    return Response(
        content=webdav.error_body(HTTPStatus.UNAUTHORIZED, "Unauthorized"),
        status_code=HTTPStatus.UNAUTHORIZED,
        media_type=XML_MEDIA_TYPE,
        headers={"WWW-Authenticate": 'Basic realm="CyberFS"'},
    )


def _refuse(status: int, reason: str) -> Response:
    return Response(
        content=webdav.error_body(status, reason),
        status_code=status,
        media_type=XML_MEDIA_TYPE,
    )


def _status_for(exc: CyberFSError) -> tuple[int, str]:
    """Map a domain error onto a WebDAV status.

    Deliberately explicit rather than reusing the REST mapping: WebDAV clients
    act on these codes, and `507` for a full quota is what tells one to stop
    rather than retry.
    """
    if isinstance(exc, NotFoundError):
        return HTTPStatus.NOT_FOUND, "Not Found"
    if isinstance(exc, PermissionDeniedError):
        return HTTPStatus.FORBIDDEN, "Forbidden"
    if isinstance(exc, QuotaExceededError):
        return _INSUFFICIENT_STORAGE, "Insufficient Storage"
    if isinstance(exc, NameTakenError):
        return HTTPStatus.PRECONDITION_FAILED, "Precondition Failed"
    if isinstance(exc, ValidationError):
        return HTTPStatus.BAD_REQUEST, "Bad Request"
    logger.error("webdav_request_failed", error_type=type(exc).__name__)
    return HTTPStatus.INTERNAL_SERVER_ERROR, "Internal Server Error"


def _ensure_secure(request: Request, requires_tls: bool) -> None:
    if not requires_tls:
        return
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded or request.url.scheme
    if scheme != "https":
        raise PermissionDeniedError("WebDAV requires TLS")


async def _authenticate(request: Request, uow: UnitOfWork) -> User:
    authenticator: WebDavAuthenticator = request.app.state.webdav_authentication
    provisioning: ProvisioningService = request.app.state.provisioning
    key = await authenticator.authenticate(uow, request.headers.get("authorization"))
    # `principal_from_key` strips administrator status by construction, so a
    # leaked WebDAV credential can never wield admin.
    user = await provisioning.resolve(uow, principal_from_key(key))
    await uow.commit()
    return user


async def _resolve(uow: UnitOfWork, user: User, path: str) -> Node:
    """Walk the path segment by segment from the caller's own root.

    One `get_child_by_name` per segment: WebDAV addresses nodes by path while
    CyberFS stores an adjacency list. The walk starts at the caller's root, so a
    path can never leave their tree, and a trashed node is absent because
    `get_child_by_name` ignores trashed rows.
    """
    node = await uow.nodes.get(user.root_folder_id)
    if node is None:
        raise NotFoundError("no root for this caller")
    segments = [unquote(part) for part in path.split("/") if part]
    if len(segments) > _MAX_SEGMENTS:
        raise ValidationError("path is too deep")
    for segment in segments:
        child = await uow.nodes.get_child_by_name(node.id, normalize_name(segment))
        if child is None or child.is_deleted:
            raise NotFoundError("no such resource", name=segment)
        node = child
    return node


#: The tree depth limit bounds the walk; a longer path is refused rather than
#: walked one query at a time.
_MAX_SEGMENTS = 64


def _split(path: str) -> tuple[str, str]:
    cleaned = path.strip("/")
    if "/" not in cleaned:
        return "", cleaned
    parent, _, name = cleaned.rpartition("/")
    return parent, name


def _destination_path(destination: str, base_path: str) -> str | None:
    """The `Destination` header as a path inside this surface, or None.

    Clients send an absolute URL. A destination outside the base path is refused
    rather than reinterpreted: guessing what a client meant by an unrelated URL
    is how a MOVE ends up somewhere nobody asked for.
    """
    raw = urlsplit(destination).path or destination
    decoded = unquote(raw)
    prefix = base_path.rstrip("/")
    if not decoded.startswith(prefix):
        return None
    return decoded[len(prefix) :].strip("/")


def _content_type(request: Request) -> str | None:
    value = request.headers.get("content-type")
    return value.split(";")[0].strip() if value else None


def _page_limit(request: Request) -> int:
    return int(request.app.state.settings.page_size_max)


def _nodes(request: Request) -> NodeService:
    service: NodeService = request.app.state.nodes
    return service


def _content(request: Request) -> ContentService:
    service: ContentService = request.app.state.content
    return service
