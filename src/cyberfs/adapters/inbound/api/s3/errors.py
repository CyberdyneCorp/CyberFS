"""Render a failure as an S3 ``<Error>`` document, never RFC 7807.

Any request to the S3 surface that fails must answer in S3's XML shape with a
`Code`, a `Message`, and the request id -- an unmodified client cannot read a
CyberFS problem document (`s3-compatibility/spec.md`, "Errors use the S3 XML
shape"). The domain error family already carries `s3_code`/`s3_status` on every
S3-relevant failure; anything else is mapped here by semantic type so a
permission or lookup error that surfaces without them still speaks S3.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import Response

from cyberfs.domain import errors as err
from cyberfs.infrastructure.logging import current_request_id, get_logger

S3_XML_MEDIA_TYPE = "application/xml"

logger = get_logger(__name__)

#: Fallback mapping for a domain error that reaches the S3 surface without its
#: own `s3_code` -- most specific type first.
_FALLBACK: tuple[tuple[type[err.CyberFSError], str, int], ...] = (
    (err.PermissionDeniedError, "AccessDenied", 403),
    (err.QuotaExceededError, "QuotaExceeded", 403),
    (err.NotFoundError, "NoSuchKey", 404),
    (err.ValidationError, "InvalidArgument", 400),
)


def s3_error_response(exc: Exception) -> Response:
    """Map any exception to an S3 ``<Error>`` response."""
    from cyberfs.adapters.inbound.api.s3.xml import error_document

    code, status, message = _classify(exc)
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        logger.error("s3_request_failed", error_type=type(exc).__name__, code=code)
    body = error_document(code, message, request_id=current_request_id())
    return Response(content=body, status_code=status, media_type=S3_XML_MEDIA_TYPE)


def _classify(exc: Exception) -> tuple[str, int, str]:
    """Resolve an exception to its S3 code, HTTP status, and safe message."""
    s3_code = getattr(exc, "s3_code", None)
    s3_status = getattr(exc, "s3_status", None)
    if isinstance(s3_code, str) and isinstance(s3_status, int):
        return s3_code, s3_status, _message(exc)
    if isinstance(exc, err.CyberFSError):
        for error_type, code, status in _FALLBACK:
            if isinstance(exc, error_type):
                return code, status, _message(exc)
    # An unexpected exception may quote content or internals; never echo it.
    return "InternalError", 500, "We encountered an internal error. Please try again."


def _message(exc: Exception) -> str:
    if isinstance(exc, err.CyberFSError):
        return exc.message
    return "We encountered an internal error. Please try again."
