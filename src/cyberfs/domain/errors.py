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
    #: The HTTP status the S3 `<Error>` document is returned with. Set here so
    #: the phase-6 XML mapper has an explicit status per error rather than
    #: re-deriving one, and so members of the family that do not subclass this
    #: base (a 400 request-shape error, a 404 missing bucket) can still declare
    #: it via `hasattr(exc, "s3_code")`.
    s3_status = 403


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


class BucketManagementRefusedError(S3RequestError):
    """`CreateBucket`/`DeleteBucket`: buckets correspond to users, not clients."""

    code = "bucket_management_refused"
    title = "Buckets are not client-managed"
    s3_code = "AccessDenied"


class S3NotImplementedError(S3RequestError):
    """An S3 operation CyberFS does not implement.

    Reported as `NotImplemented`/501 rather than a CyberFS error shape, so an
    unmodified client learns the operation is unsupported instead of receiving a
    permission error (`s3-compatibility/spec.md`, "An unsupported operation is
    reported as such"). It sits in the S3 family only to carry `s3_code`/
    `s3_status`; the denial semantics of the base are not what apply here.
    """

    code = "s3_not_implemented"
    title = "Operation is not implemented"
    s3_code = "NotImplemented"
    s3_status = 501


# --- lookup ----------------------------------------------------------------


class NotFoundError(CyberFSError):
    code = "not_found"
    title = "Resource not found"


class RecipientUnknownError(NotFoundError):
    code = "recipient_unknown"
    title = "Recipient is not a known user"


class NoSuchBucketError(NotFoundError):
    """A bucket that is not the caller's own.

    Subclasses `NotFoundError` so a foreign subject's bucket and a bucket that
    never existed are indistinguishable by construction -- bucket names must not
    become a user directory (`s3-compatibility/spec.md`, "Another user's bucket
    is not reachable").
    """

    code = "no_such_bucket"
    title = "The specified bucket does not exist"
    s3_code = "NoSuchBucket"
    s3_status = 404


class NoSuchKeyError(NotFoundError):
    """An object key that names no reachable node in the caller's view.

    A node the caller may not see is `NoSuchKey`, identically to one that never
    existed, so existence is not disclosed (`file-storage/spec.md`, "Download
    denied without permission").
    """

    code = "no_such_key"
    title = "The specified key does not exist"
    s3_code = "NoSuchKey"
    s3_status = 404


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


class AmbiguousS3CredentialsError(ValidationError):
    """A request presenting both a SigV4 signature and a bearer token.

    A request-shape error (400), not a permission failure: the caller must
    choose one credential rather than have the server silently pick
    (`s3-compatibility/spec.md`, "Both credentials on one request is refused").
    Carries `s3_code`/`s3_status` so the S3 surface can render it in S3's shape.
    """

    code = "s3_ambiguous_credentials"
    title = "Present one credential, not both"
    s3_code = "InvalidRequest"
    s3_status = 400


class InvalidArgumentError(ValidationError):
    """A malformed S3 list parameter -- a garbage continuation token, say.

    A request-shape error (400) rendered in S3's dialect as `InvalidArgument`.
    A continuation token is only an opaque resume cursor over the caller's own
    authorized view, so a tampered one cannot widen access; it is simply
    rejected rather than trusted.
    """

    code = "s3_invalid_argument"
    title = "An argument is not valid"
    s3_code = "InvalidArgument"
    s3_status = 400


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
    # The S3 surface renders a quota rejection as `QuotaExceeded`/403 and stores
    # neither object nor metadata (`s3-compatibility/spec.md`, "An upload beyond
    # quota is refused").
    s3_code = "QuotaExceeded"
    s3_status = 403


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
