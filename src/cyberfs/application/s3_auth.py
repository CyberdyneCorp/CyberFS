"""Verifying an AWS Signature V4 on the S3 surface.

The verifier reproduces the exact canonical request the client signed, derives
the same signing key from the access key's secret, and compares the two
signatures in constant time. Its non-obvious obligations, each pinned by
`s3-compatibility/spec.md`:

* an *unknown* access key and a *bad signature* must be indistinguishable by
  timing -- so an unknown or revoked key still runs the same AES-GCM unseal,
  signing-key, signature, and compare work against a fixed placeholder secret
  before it is refused, and both outcomes are 403s;
* a signed `x-amz-date` beyond `S3_CLOCK_SKEW_SECONDS` is refused
  (`RequestTimeTooSkewed`), bounding replay of a captured signature;
* a body whose digest does not match the signed `x-amz-content-sha256` is
  refused, so a valid signature cannot be replayed over swapped content
  (`UNSIGNED-PAYLOAD` opts out, as the spec allows);
* repeated failures from one source IP are rate limited, reusing the same
  fixed-window limiter the REST auth failures use.

Scope boundary: this returns the verified `S3AccessKey` only. Mapping it to a
`Principal` -- and stripping administrator status from a key-authenticated
caller -- is deferred to phase 5; here the guarantee is solely that the
resolved owner is `key.owner_subject`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cyberfs.domain.audit import AuditAction, AuditProtocol, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import (
    ContentHashMismatchError,
    RateLimitedError,
    RequestSkewedError,
    S3RequestError,
    SignatureMismatchError,
    UnknownAccessKeyError,
)
from cyberfs.domain.ports.crypto import KeyProvider
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.domain.s3 import sigv4
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.authorization import AuthorizationHeader, parse_authorization_header
from cyberfs.domain.s3.request import S3Request
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

SIGNATURE_FAILURE_WINDOW = timedelta(minutes=1)

#: A fixed, never-issued secret. It is sealed once at construction so an unknown
#: or revoked key can incur the same AES-GCM unseal and signing-key/signature
#: work as a known one, keeping the two rejections timing-equivalent. Its value
#: is irrelevant -- only that the identical work happens.
_DUMMY_SECRET = "cyberfs/unknown-access-key/constant-time-placeholder-secret"  # noqa: S105


class S3SignatureVerifier:
    """Authenticates a signed S3 request to the access key that signed it."""

    def __init__(
        self,
        keys: KeyProvider,
        failure_limiter: FixedWindowLimiter,
        *,
        region: str,
        clock_skew_seconds: int,
    ) -> None:
        self._keys = keys
        self._limiter = failure_limiter
        self._region = region
        self._skew = timedelta(seconds=clock_skew_seconds)
        # A placeholder secret, sealed once under the current master key, so an
        # unknown or revoked key can be unsealed with the same AES-GCM cost a
        # known key incurs -- closing the "key exists" timing side channel.
        self._dummy_sealed = keys.seal_secret(_DUMMY_SECRET.encode("utf-8"))
        self._dummy_master_key_id = keys.master_key_id

    async def verify(
        self, uow: UnitOfWork, request: S3Request, *, now: datetime | None = None
    ) -> S3AccessKey:
        """Return the active access key that validly signed `request`.

        Raises `RateLimitedError` when the source IP is throttled, and an
        `S3RequestError` (403) -- `SignatureDoesNotMatch`, `InvalidAccessKeyId`,
        `RequestTimeTooSkewed`, or the content-hash mismatch -- otherwise.
        """
        moment = now or utcnow()
        await self._ensure_not_rate_limited(uow, request.source_ip, moment)
        try:
            return await self._verify(uow, request, moment)
        except S3RequestError as exc:
            await self._record_failure(uow, request, moment, exc)
            raise

    async def _verify(self, uow: UnitOfWork, request: S3Request, now: datetime) -> S3AccessKey:
        auth = parse_authorization_header(request.headers.get("authorization"))
        if auth is None:
            raise SignatureMismatchError("authorization header is malformed")
        self._ensure_within_skew(request.amz_date, now)
        self._ensure_content_matches(request)

        key = await uow.s3_keys.get_by_key_id(auth.access_key_id)
        if key is None or not key.is_active:
            # Burn the same unseal + signing-key + signature work against a
            # sealed placeholder so an unknown or revoked key is
            # indistinguishable by timing from a merely bad signature.
            dummy_secret = self._unseal(self._dummy_sealed, self._dummy_master_key_id)
            self._compute_signature(dummy_secret, auth, request)
            raise UnknownAccessKeyError("access key is not recognised")

        secret = self._unseal(key.sealed_secret, key.secret_master_key_id)
        computed = self._compute_signature(secret, auth, request)
        if not sigv4.verify(computed, auth.signature):
            raise SignatureMismatchError("request signature does not match")

        await uow.s3_keys.update(key.with_last_used(now))
        logger.info("s3_request_authenticated", key_id=key.key_id, subject=key.owner_subject)
        return key

    def _unseal(self, sealed: bytes, master_key_id: str) -> str:
        return self._keys.unseal_secret(sealed, master_key_id=master_key_id).decode("utf-8")

    def _compute_signature(self, secret: str, auth: AuthorizationHeader, request: S3Request) -> str:
        request_string = sigv4.canonical_request(
            request.method,
            request.path,
            request.query,
            request.headers,
            auth.signed_headers,
            request.content_sha256,
        )
        scope = sigv4.credential_scope(auth.date_stamp, auth.region, auth.service)
        to_sign = sigv4.string_to_sign(request.amz_date, scope, request_string)
        key = sigv4.signing_key(secret, auth.date_stamp, auth.region, auth.service)
        return sigv4.signature(key, to_sign)

    def _ensure_within_skew(self, amz_date: str, now: datetime) -> None:
        signed_at = sigv4.parse_amz_date(amz_date)
        if abs(now - signed_at) > self._skew:
            raise RequestSkewedError("request time is outside the permitted skew")

    def _ensure_content_matches(self, request: S3Request) -> None:
        if request.content_sha256 == sigv4.UNSIGNED_PAYLOAD:
            return
        if request.body_sha256 is None:
            return
        if request.content_sha256 != request.body_sha256:
            raise ContentHashMismatchError("body does not match the signed content hash")

    async def _ensure_not_rate_limited(
        self, uow: UnitOfWork, source_ip: str | None, now: datetime
    ) -> None:
        if source_ip is None or not self._limiter.is_limited(source_ip, now):
            return
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.AUTH_RATE_LIMITED,
                occurred_at=now,
                protocol=AuditProtocol.S3,
                source_ip=source_ip,
                reason="too_many_signature_failures",
            )
        )
        raise RateLimitedError(
            "too many signature failures",
            retry_after_seconds=int(self._limiter.retry_after(source_ip, now).total_seconds()),
        )

    async def _record_failure(
        self, uow: UnitOfWork, request: S3Request, now: datetime, exc: S3RequestError
    ) -> None:
        if request.source_ip is not None:
            self._limiter.record(request.source_ip, now)
        await uow.audit.add(
            AuditRecord(
                action=AuditAction.AUTH_DENIED,
                occurred_at=now,
                protocol=AuditProtocol.S3,
                source_ip=request.source_ip,
                reason=exc.code,
            )
        )
