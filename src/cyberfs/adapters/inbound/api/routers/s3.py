"""The S3-compatible HTTP surface.

S3 disambiguates operations by method and query parameters over a `{bucket}` /
`{bucket}/{key}` path, not by distinct routes, so this router accepts each shape
and dispatches. Two deliberate choices shape it:

* **Errors are S3 XML, always.** A failure anywhere -- including authentication
  -- must answer in S3's ``<Error>`` shape, never RFC 7807. So each handler
  authenticates *in its body* inside a single ``try`` that renders any exception
  through `s3_error_response`; nothing is pushed into a dependency that would
  fail as problem+json before the handler runs.
* **Every operation is delegated.** The router parses the request and calls
  `S3ObjectService`, which in turn calls the same `ContentService`/`NodeService`
  the REST surface uses -- permission, quota, versioning, encryption, and trash
  behaviour are identical by construction.

Mounted only when ``S3_API_ENABLED`` (`s3-compatibility/spec.md`).
"""

from __future__ import annotations

from email.utils import formatdate
from http import HTTPStatus
from urllib.parse import unquote

from fastapi import APIRouter, Request, Response

from cyberfs.adapters.inbound.api.dependencies import UnitOfWorkDep
from cyberfs.adapters.inbound.api.routers.content import stream_response
from cyberfs.adapters.inbound.api.s3.errors import S3_XML_MEDIA_TYPE, s3_error_response
from cyberfs.adapters.inbound.api.s3.xml import (
    copy_object_result,
    delete_result,
    list_all_my_buckets,
    list_bucket_result,
    parse_delete_request,
)
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.s3_authentication import S3Authenticator
from cyberfs.application.s3_objects import HeadInfo, S3ObjectService
from cyberfs.domain.errors import InvalidArgumentError, S3NotImplementedError
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.s3.listing import MAX_KEYS, ListRequest
from cyberfs.domain.s3.namespace import S3Target, resolve_key
from cyberfs.domain.s3.request import S3Request
from cyberfs.domain.users import User

#: Sub-resource query keys that name an S3 operation CyberFS does not implement.
#: Presence of any one turns the request into a `NotImplemented`, rather than
#: being mistaken for a list or an object read.
_UNSUPPORTED_SUBRESOURCES = frozenset(
    {
        "acl",
        "tagging",
        "uploads",
        "uploadid",
        "versioning",
        "versions",
        "policy",
        "cors",
        "lifecycle",
        "website",
        "logging",
        "location",
        "replication",
        "encryption",
        "object-lock",
        "legal-hold",
        "retention",
        "torrent",
        "restore",
        "select",
        "accelerate",
        "analytics",
        "inventory",
        "metrics",
        "notification",
        "ownershipcontrols",
        "publicaccessblock",
        "requestpayment",
    }
)


def create_s3_router(base_path: str) -> APIRouter:
    """Build the S3 router rooted at `base_path` (e.g. ``/s3``)."""
    router = APIRouter(prefix=base_path, tags=["s3"])

    @router.get("/")
    async def list_buckets(request: Request, uow: UnitOfWorkDep) -> Response:
        try:
            user = await _authenticate(request, uow)
            body = list_all_my_buckets(user.subject, _service(request).list_buckets(user))
            return Response(content=body, media_type=S3_XML_MEDIA_TYPE)
        except Exception as exc:  # every failure on the S3 surface is rendered as S3 XML
            return s3_error_response(exc)

    @router.api_route("/{bucket}", methods=["GET", "HEAD", "PUT", "POST", "DELETE"])
    async def bucket(bucket: str, request: Request, uow: UnitOfWorkDep) -> Response:
        try:
            user = await _authenticate(request, uow)
            return await _dispatch_bucket(request, uow, user, bucket)
        except Exception as exc:  # every failure on the S3 surface is rendered as S3 XML
            return s3_error_response(exc)

    @router.api_route("/{bucket}/{key:path}", methods=["GET", "HEAD", "PUT", "POST", "DELETE"])
    async def obj(bucket: str, key: str, request: Request, uow: UnitOfWorkDep) -> Response:
        try:
            user = await _authenticate(request, uow)
            return await _dispatch_object(request, uow, user, bucket, key)
        except Exception as exc:  # every failure on the S3 surface is rendered as S3 XML
            return s3_error_response(exc)

    return router


# --- dispatch --------------------------------------------------------------


async def _dispatch_bucket(request: Request, uow: UnitOfWork, user: User, bucket: str) -> Response:
    # `DeleteObjects` is the one POST the bucket accepts, keyed by `?delete`; it
    # is handled before `_ensure_supported`, which would otherwise never see a
    # subresource it implements.
    if request.method == "POST" and "delete" in request.query_params:
        return await _delete_objects(request, uow, user, bucket)
    _ensure_supported(request)
    if request.method == "HEAD":
        _service(request).head_bucket(user, bucket)
        return Response(status_code=HTTPStatus.OK)
    if request.method == "GET":
        req = _parse_list_request(request)
        result = await _service(request).list_objects(uow, user, bucket, req)
        return Response(
            content=list_bucket_result(bucket, result, req), media_type=S3_XML_MEDIA_TYPE
        )
    raise S3NotImplementedError(f"{request.method} on a bucket is not implemented")


async def _delete_objects(request: Request, uow: UnitOfWork, user: User, bucket: str) -> Response:
    """`DeleteObjects`: soft-delete a batch, reporting each key independently."""
    keys, quiet = parse_delete_request(await request.body())
    outcomes = await _service(request).delete_objects(uow, user, bucket, keys)
    await uow.commit()
    return Response(content=delete_result(outcomes, quiet=quiet), media_type=S3_XML_MEDIA_TYPE)


async def _dispatch_object(
    request: Request, uow: UnitOfWork, user: User, bucket: str, key: str
) -> Response:
    _ensure_supported(request)
    # A foreign bucket is `NoSuchBucket` and a malformed shared address is
    # `InvalidArgument`; both are raised here and rendered as S3 XML.
    target = resolve_key(user.subject, bucket, key)
    if request.method == "HEAD":
        return _head_response(await _service(request).head_object(uow, user, target))
    if request.method == "GET":
        # Object-version parameters are ignored: CyberFS versioning is not S3
        # object versioning, so a read always serves the current version.
        plan = await _service(request).get_object(
            uow, user, target, range_header=request.headers.get("range")
        )
        # The read emits a download audit record; commit it before streaming.
        await uow.commit()
        return stream_response(plan)
    if request.method == "PUT":
        return await _put_or_copy(request, uow, user, target)
    if request.method == "DELETE":
        await _service(request).delete_object(uow, user, target)
        await uow.commit()
        return Response(status_code=HTTPStatus.NO_CONTENT)
    raise S3NotImplementedError(f"{request.method} on an object is not implemented")


async def _put_or_copy(request: Request, uow: UnitOfWork, user: User, target: S3Target) -> Response:
    """A `PUT` is a `CopyObject` when it names a copy source, else a `PutObject`."""
    copy_source = request.headers.get("x-amz-copy-source")
    if copy_source is not None:
        return await _copy_object(request, uow, user, target, copy_source)
    return await _put_object(request, uow, user, target)


async def _put_object(request: Request, uow: UnitOfWork, user: User, target: S3Target) -> Response:
    node = await _service(request).put_object(
        uow,
        user,
        target,
        request.stream(),
        content_type=request.headers.get("content-type"),
        declared_length=_content_length(request),
    )
    await uow.commit()
    return Response(status_code=HTTPStatus.OK, headers={"ETag": node.etag})


async def _copy_object(
    request: Request, uow: UnitOfWork, user: User, target: S3Target, copy_source: str
) -> Response:
    bucket, key = _split_copy_source(copy_source)
    source = resolve_key(user.subject, bucket, key)
    node = await _service(request).copy_object(uow, user, source, target)
    await uow.commit()
    return Response(
        content=copy_object_result(node.etag, node.updated_at),
        media_type=S3_XML_MEDIA_TYPE,
    )


def _content_length(request: Request) -> int | None:
    """The declared body length, or None when the client sent no header."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidArgumentError("content-length is not a number") from exc


def _split_copy_source(copy_source: str) -> tuple[str, str]:
    """Split an ``x-amz-copy-source`` header into its bucket and key.

    The header is ``/bucket/key`` or ``bucket/key`` and may carry a trailing
    ``?versionId=…``, which is ignored -- CyberFS versioning is not S3 object
    versioning, so a copy always sources the current version.
    """
    source = unquote(copy_source.split("?", 1)[0]).lstrip("/")
    bucket, _, key = source.partition("/")
    if not bucket or not key:
        raise InvalidArgumentError("the copy source is not a valid bucket/key")
    return bucket, key


# --- request plumbing ------------------------------------------------------


async def _authenticate(request: Request, uow: UnitOfWork) -> User:
    """Resolve the caller to a provisioned user, committing the first touch.

    No `require_fresh`: S3 has no share operations, and revocation is enforced by
    synchronous grant-cache invalidation, so an ordinary read never takes an
    introspection round trip.
    """
    authenticator: S3Authenticator = request.app.state.s3_authentication
    provisioning: ProvisioningService = request.app.state.provisioning
    authed = await authenticator.authenticate(uow, _build_s3_request(request))
    user = await provisioning.resolve(uow, authed.principal)
    await uow.commit()
    return user


def _build_s3_request(request: Request) -> S3Request:
    headers = dict(request.headers.items())
    return S3Request(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        headers=headers,
        amz_date=headers.get("x-amz-date", ""),
        content_sha256=headers.get("x-amz-content-sha256", ""),
        # A read is not drained, so the body digest is not recomputed; the signed
        # `x-amz-content-sha256` still binds the signature to the declared body.
        body_sha256=None,
        source_ip=request.client.host if request.client else None,
    )


def _parse_list_request(request: Request) -> ListRequest:
    params = request.query_params
    return ListRequest(
        prefix=params.get("prefix", ""),
        delimiter=params.get("delimiter", ""),
        max_keys=_int(params.get("max-keys"), MAX_KEYS),
        continuation_token=params.get("continuation-token"),
        start_after=params.get("start-after"),
    )


def _int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidArgumentError("max-keys is not a number") from exc


def _ensure_supported(request: Request) -> None:
    """Refuse an S3 sub-resource operation CyberFS does not implement."""
    for name in request.query_params:
        if name.lower() in _UNSUPPORTED_SUBRESOURCES:
            raise S3NotImplementedError(f"the {name} operation is not implemented")


def _head_response(info: HeadInfo) -> Response:
    headers = {
        "Content-Length": str(info.size),
        "ETag": info.etag,
        "Last-Modified": formatdate(info.last_modified.timestamp(), usegmt=True),
        "Accept-Ranges": "bytes",
    }
    return Response(status_code=HTTPStatus.OK, media_type=info.content_type, headers=headers)


def _service(request: Request) -> S3ObjectService:
    service: S3ObjectService = request.app.state.s3_objects
    return service
