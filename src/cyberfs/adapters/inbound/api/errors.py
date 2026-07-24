"""Map domain errors to RFC 7807 problem details.

HTTP lives here, not in the domain. Responses carry the stable domain `code`
so clients branch on that rather than on prose, and the request id so a report
can be traced to a log line.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cyberfs.domain import errors as err
from cyberfs.infrastructure.logging import current_request_id, get_logger

PROBLEM_JSON = "application/problem+json"

logger = get_logger(__name__)

# Most specific first: the first class the error is an instance of wins.
_STATUS_BY_ERROR: tuple[tuple[type[err.CyberFSError], HTTPStatus], ...] = (
    (err.TokenExpiredError, HTTPStatus.UNAUTHORIZED),
    (err.InvalidTokenError, HTTPStatus.UNAUTHORIZED),
    (err.AuthenticationError, HTTPStatus.UNAUTHORIZED),
    (err.PermissionDeniedError, HTTPStatus.FORBIDDEN),
    (err.RateLimitedError, HTTPStatus.TOO_MANY_REQUESTS),
    (err.RecipientUnknownError, HTTPStatus.NOT_FOUND),
    (err.NotFoundError, HTTPStatus.NOT_FOUND),
    (err.ConflictError, HTTPStatus.CONFLICT),
    (err.PreconditionFailedError, HTTPStatus.PRECONDITION_FAILED),
    (err.PayloadTooLargeError, HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
    (err.ValidationError, HTTPStatus.UNPROCESSABLE_ENTITY),
    (err.QuotaExceededError, HTTPStatus.INSUFFICIENT_STORAGE),
    (err.DependencyUnavailableError, HTTPStatus.SERVICE_UNAVAILABLE),
    (err.KeyUnavailableError, HTTPStatus.INTERNAL_SERVER_ERROR),
    (err.IntegrityFailureError, HTTPStatus.INTERNAL_SERVER_ERROR),
)

#: Failures that mean stored data is wrong, not that a caller misbehaved.
_ALERT_CODES = frozenset({err.IntegrityFailureError.code, err.KeyUnavailableError.code})


def status_for(error: err.CyberFSError) -> HTTPStatus:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return status
    return HTTPStatus.INTERNAL_SERVER_ERROR


def problem_response(error: err.CyberFSError, status: HTTPStatus) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"https://cyberfs.cyberdynecorp.ai/errors/{error.code}",
        "title": error.title,
        "status": int(status),
        "code": error.code,
        "detail": error.message,
    }
    if (request_id := current_request_id()) is not None:
        body["request_id"] = request_id
    return JSONResponse(status_code=int(status), content=body, media_type=PROBLEM_JSON)


async def handle_domain_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, err.CyberFSError)
    status = status_for(exc)
    if exc.code in _ALERT_CODES:
        # `file-storage/spec.md` and `content-encryption/spec.md` both require an
        # alert-level log when stored content fails authentication.
        logger.error("storage_integrity_alert", code=exc.code, **exc.context)
    elif status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        logger.error("request_failed", code=exc.code, **exc.context)
    return problem_response(exc, status)


async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    """Never surface an unexpected exception's text -- it may quote content."""
    logger.exception("unhandled_exception", error_type=type(exc).__name__)
    return problem_response(err.CyberFSError(), HTTPStatus.INTERNAL_SERVER_ERROR)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(err.CyberFSError, handle_domain_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
