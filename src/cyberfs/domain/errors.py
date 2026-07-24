"""Domain error taxonomy.

Errors are semantic, not HTTP -- the inbound adapter maps them to status
codes. The `code` on each class is the stable, machine-readable identifier
that appears in API responses and audit records; the codes named in the specs
(`name_taken`, `would_create_cycle`, `token_expired`, `integrity_failure`, …)
are defined here.
"""

from __future__ import annotations

from typing import Any


class CyberFSError(Exception):
    """Base for every expected failure."""

    code = "internal_error"
    #: Safe to show a caller. Subclasses that could leak internals override it.
    title = "Internal error"

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.title)
        self.message = message or self.title
        self.context = context


# --- authentication / authorization ---------------------------------------


class AuthenticationError(CyberFSError):
    code = "unauthenticated"
    title = "Authentication required"


class TokenExpiredError(AuthenticationError):
    code = "token_expired"
    title = "Access token has expired"


class InvalidTokenError(AuthenticationError):
    code = "invalid_token"
    title = "Access token is not valid"


class PermissionDeniedError(CyberFSError):
    code = "permission_denied"
    title = "Insufficient permission"


class RateLimitedError(CyberFSError):
    code = "rate_limited"
    title = "Too many requests"


# --- s3 signature-v4 -------------------------------------------------------
#
# A signature-verification failure is a permission failure, so each of these
# subclasses `PermissionDeniedError` and renders 403 if it ever reaches the
# REST error mapper unchanged. Every one also carries an `s3_code`: phase 6
# serialises that into the S3 `<Error>` XML document (its own status and
# message), so the S3 surface speaks S3's dialect while the domain keeps a
# single semantic taxonomy. Only the verifier and these types live here; the
# XML surface is deliberately built later.


class S3RequestError(PermissionDeniedError):
    """Base for a rejected S3 request. Defaults to the generic S3 denial."""

    code = "s3_error"
    #: The AWS S3 error code phase 6 serialises into the `<Error>` document.
    s3_code = "AccessDenied"


class SignatureMismatchError(S3RequestError):
    code = "signature_mismatch"
    title = "Request signature does not match"
    s3_code = "SignatureDoesNotMatch"


class UnknownAccessKeyError(S3RequestError):
    code = "unknown_access_key"
    title = "Access key is not recognised"
    s3_code = "InvalidAccessKeyId"


class RequestSkewedError(S3RequestError):
    code = "request_skewed"
    title = "Request time is too far from the server clock"
    s3_code = "RequestTimeTooSkewed"


class ContentHashMismatchError(S3RequestError):
    code = "content_mismatch"
    title = "Body does not match the signed content hash"
    s3_code = "XAmzContentSHA256Mismatch"


class S3AccessDeniedError(S3RequestError):
    code = "s3_access_denied"
    title = "Access denied"
    s3_code = "AccessDenied"


# --- lookup ----------------------------------------------------------------


class NotFoundError(CyberFSError):
    code = "not_found"
    title = "Resource not found"


class RecipientUnknownError(NotFoundError):
    code = "recipient_unknown"
    title = "Recipient is not a known user"


# --- conflict --------------------------------------------------------------


class ConflictError(CyberFSError):
    code = "conflict"
    title = "Conflicting state"


class NameTakenError(ConflictError):
    code = "name_taken"
    title = "A sibling with that name already exists"


class WouldCreateCycleError(ConflictError):
    code = "would_create_cycle"
    title = "Move would make a folder its own ancestor"


class CrossOwnerMoveError(ConflictError):
    code = "cross_owner_move"
    title = "Move would cross an ownership boundary"


class CannotRevokeOwnerError(ConflictError):
    code = "cannot_revoke_owner"
    title = "The owner's own access cannot be revoked"


class CannotShareWithSelfError(ConflictError):
    code = "cannot_share_with_self"
    title = "Cannot share with yourself"


# --- request shape ---------------------------------------------------------


class ValidationError(CyberFSError):
    code = "validation_error"
    title = "Request is not valid"


class PreconditionFailedError(CyberFSError):
    code = "precondition_failed"
    title = "Precondition failed"


class PayloadTooLargeError(CyberFSError):
    code = "payload_too_large"
    title = "Payload exceeds the maximum upload size"


# --- storage ---------------------------------------------------------------


class QuotaExceededError(CyberFSError):
    code = "quota_exceeded"
    title = "Storage quota exceeded"


class IntegrityFailureError(CyberFSError):
    code = "integrity_failure"
    # Deliberately opaque: `content-encryption/spec.md` forbids leaking nonces,
    # tags, or ciphertext fragments through an error path.
    title = "Stored content failed its integrity check"


class KeyUnavailableError(CyberFSError):
    code = "key_unavailable"
    title = "Encryption key is unavailable"


class DependencyUnavailableError(CyberFSError):
    code = "dependency_unavailable"
    title = "A required dependency is unavailable"


class CacheUnavailableError(CyberFSError):
    """The cache could not serve or accept an operation.

    Not an API-facing error: reads swallow it (a broken cache is a miss) and
    only the invalidation path escalates, because a missed invalidation can
    leave a revoked permission cached.
    """

    code = "cache_unavailable"
    title = "Cache is unavailable"
