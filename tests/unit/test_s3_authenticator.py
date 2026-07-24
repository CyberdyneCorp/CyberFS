"""The S3 SigV4 verifier -- every scenario in the signature requirement.

Covers acceptance, bad-signature and unknown-key rejection (and that the two
are indistinguishable in shape and equivalent in work), immediate effect of
revocation, clock-skew rejection, content-hash verification including the
``UNSIGNED-PAYLOAD`` opt-out, malformed headers, last-used stamping, and
per-source-IP failure rate limiting.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from cyberfs.application.s3_auth import S3SignatureVerifier
from cyberfs.domain.audit import AuditAction, AuditProtocol
from cyberfs.domain.errors import (
    ContentHashMismatchError,
    PermissionDeniedError,
    RateLimitedError,
    RequestSkewedError,
    S3AccessDeniedError,
    S3RequestError,
    SignatureMismatchError,
    UnknownAccessKeyError,
)
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.domain.s3 import presigned, sigv4
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.request import S3Request

from .fakes import FakeKeyProvider, FakeUnitOfWork

#: Sentinel so a caller can force `body_sha256=None` (a streamed body not yet
#: hashed) distinctly from "not supplied, hash the body for me".
_COMPUTE = object()

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
REGION = "us-east-1"
SERVICE = "s3"
SECRET = "cyberfs-test-secret-access-key-value"
ACCESS_KEY_ID = "AKIATEST0000000000CI"
OWNER = "user-1"
SOURCE_IP = "198.51.100.7"


class CountingKeyProvider(FakeKeyProvider):
    """Counts unseal calls, so we can prove the unknown-key path unseals a
    placeholder exactly as often as the bad-signature path unseals the real
    key -- the two are timing-equivalent. (Construction only seals, so the
    count reflects only the unseals performed while serving a request.)"""

    def __init__(self) -> None:
        super().__init__()
        self.unseal_calls = 0

    def unseal_secret(self, sealed: bytes, *, master_key_id: str) -> bytes:
        self.unseal_calls += 1
        return super().unseal_secret(sealed, master_key_id=master_key_id)


def _make_verifier(
    provider: FakeKeyProvider,
    *,
    limit: int = 5,
    clock_skew_seconds: int = 300,
) -> S3SignatureVerifier:
    return S3SignatureVerifier(
        keys=provider,
        failure_limiter=FixedWindowLimiter(limit=limit, window=timedelta(minutes=1)),
        region=REGION,
        clock_skew_seconds=clock_skew_seconds,
    )


def _make_key(provider: FakeKeyProvider, *, revoked: bool = False) -> S3AccessKey:
    return S3AccessKey(
        key_id=ACCESS_KEY_ID,
        sealed_secret=provider.seal_secret(SECRET.encode("utf-8")),
        secret_master_key_id=provider.master_key_id,
        label="ci",
        owner_id=uuid.uuid4(),
        owner_subject=OWNER,
        created_at=NOW,
        revoked_at=NOW if revoked else None,
    )


def _signed_request(
    *,
    signed_at: datetime,
    secret: str = SECRET,
    access_key_id: str = ACCESS_KEY_ID,
    body: bytes = b"payload",
    content_sha256: str | None = None,
    body_sha256: str | object | None = _COMPUTE,
    tamper_signature: bool = False,
    source_ip: str | None = SOURCE_IP,
) -> S3Request:
    """Build a genuinely SigV4-signed request over the domain's own functions."""
    amz_date = signed_at.strftime(sigv4.AMZ_DATE_FORMAT)
    date_stamp = signed_at.strftime("%Y%m%d")
    payload_hash = (
        content_sha256 if content_sha256 is not None else hashlib.sha256(body).hexdigest()
    )
    method, path = "GET", "/reports/q3.xlsx"
    headers = {
        "host": f"{OWNER}.s3.cyberfs.local",
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed_headers = ("host", "x-amz-content-sha256", "x-amz-date")
    creq = sigv4.canonical_request(method, path, "", headers, signed_headers, payload_hash)
    scope = sigv4.credential_scope(date_stamp, REGION, SERVICE)
    to_sign = sigv4.string_to_sign(amz_date, scope, creq)
    signature = sigv4.signature(sigv4.signing_key(secret, date_stamp, REGION, SERVICE), to_sign)
    if tamper_signature:
        signature = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    headers["authorization"] = (
        f"{sigv4.ALGORITHM} "
        f"Credential={access_key_id}/{date_stamp}/{REGION}/{SERVICE}/aws4_request, "
        f"SignedHeaders={';'.join(signed_headers)}, "
        f"Signature={signature}"
    )
    if body_sha256 is _COMPUTE:
        computed_body: str | None = hashlib.sha256(body).hexdigest()
    else:
        computed_body = cast("str | None", body_sha256)
    return S3Request(
        method=method,
        path=path,
        query="",
        headers=headers,
        amz_date=amz_date,
        content_sha256=payload_hash,
        body_sha256=computed_body,
        source_ip=source_ip,
    )


async def _seed_key(uow: FakeUnitOfWork, key: S3AccessKey) -> None:
    await uow.s3_keys.add(key)


# --- acceptance ------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_signature_resolves_to_the_owning_key() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    resolved = await verifier.verify(uow, _signed_request(signed_at=NOW), now=NOW)

    assert resolved.key_id == ACCESS_KEY_ID
    assert resolved.owner_subject == OWNER


@pytest.mark.asyncio
async def test_authentication_stamps_last_used() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    await verifier.verify(uow, _signed_request(signed_at=NOW), now=NOW)

    assert uow.s3_keys.by_key_id[ACCESS_KEY_ID].last_used_at == NOW


@pytest.mark.asyncio
async def test_unsigned_payload_skips_the_body_check() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    request = _signed_request(
        signed_at=NOW,
        content_sha256=sigv4.UNSIGNED_PAYLOAD,
        body_sha256="a" * 64,  # would mismatch, but the check is opted out
    )
    resolved = await verifier.verify(uow, request, now=NOW)
    assert resolved.key_id == ACCESS_KEY_ID


@pytest.mark.asyncio
async def test_uncomputed_body_hash_defers_the_content_check() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    # A streamed body not yet drained carries no computed digest; the signature
    # still verifies and the content check is simply not applied here.
    request = _signed_request(signed_at=NOW, body_sha256=None)
    resolved = await verifier.verify(uow, request, now=NOW)
    assert resolved.key_id == ACCESS_KEY_ID


# --- rejection -------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_signature_is_refused() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    with pytest.raises(SignatureMismatchError) as excinfo:
        await verifier.verify(uow, _signed_request(signed_at=NOW, tamper_signature=True), now=NOW)
    assert excinfo.value.s3_code == "SignatureDoesNotMatch"


@pytest.mark.asyncio
async def test_unknown_key_unseals_a_placeholder_before_refusing() -> None:
    provider = CountingKeyProvider()
    uow = FakeUnitOfWork()  # no key seeded
    verifier = _make_verifier(provider)

    with pytest.raises(UnknownAccessKeyError) as excinfo:
        await verifier.verify(uow, _signed_request(signed_at=NOW), now=NOW)
    assert excinfo.value.s3_code == "InvalidAccessKeyId"
    # The unknown path unseals a fixed placeholder and burns the signing-key
    # work against it, so it costs the same AES-GCM + HMAC time as a
    # bad-signature rejection against a real key -- no "key exists" side channel.
    assert provider.unseal_calls == 1


@pytest.mark.asyncio
async def test_unknown_key_and_bad_signature_share_the_403_shape() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    with pytest.raises(S3RequestError) as bad:
        await verifier.verify(uow, _signed_request(signed_at=NOW, tamper_signature=True), now=NOW)
    with pytest.raises(S3RequestError) as unknown:
        await verifier.verify(
            uow, _signed_request(signed_at=NOW, access_key_id="AKIANOSUCHKEY0000001"), now=NOW
        )
    # Both are permission failures (403); they differ only by S3 code, as AWS does.
    assert isinstance(bad.value, PermissionDeniedError)
    assert isinstance(unknown.value, PermissionDeniedError)
    assert {bad.value.s3_code, unknown.value.s3_code} == {
        "SignatureDoesNotMatch",
        "InvalidAccessKeyId",
    }


@pytest.mark.asyncio
async def test_unknown_key_and_bad_signature_do_equal_unseal_work() -> None:
    # Regression for a "key exists" timing side channel: a known key with a bad
    # signature performs one AES-GCM unseal (of the real secret); an unknown key
    # must perform exactly one too (of the placeholder), never zero.
    bad_sig_provider = CountingKeyProvider()
    bad_uow = FakeUnitOfWork()
    await _seed_key(bad_uow, _make_key(bad_sig_provider))
    bad_verifier = _make_verifier(bad_sig_provider)
    with pytest.raises(SignatureMismatchError):
        await bad_verifier.verify(
            bad_uow, _signed_request(signed_at=NOW, tamper_signature=True), now=NOW
        )

    unknown_provider = CountingKeyProvider()
    unknown_uow = FakeUnitOfWork()  # no key seeded
    unknown_verifier = _make_verifier(unknown_provider)
    with pytest.raises(UnknownAccessKeyError):
        await unknown_verifier.verify(unknown_uow, _signed_request(signed_at=NOW), now=NOW)

    assert unknown_provider.unseal_calls == bad_sig_provider.unseal_calls == 1


@pytest.mark.asyncio
async def test_revocation_takes_effect_immediately() -> None:
    provider = CountingKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider, revoked=True))
    verifier = _make_verifier(provider)

    with pytest.raises(UnknownAccessKeyError):
        await verifier.verify(uow, _signed_request(signed_at=NOW), now=NOW)
    # A revoked key is refused on the same placeholder-unseal path as an unknown
    # one, so it stays timing-indistinguishable from a bad-signature rejection.
    assert provider.unseal_calls == 1


@pytest.mark.asyncio
async def test_stale_request_is_refused() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider, clock_skew_seconds=300)

    # Signed ten minutes ago; skew allowance is five.
    stale = _signed_request(signed_at=NOW - timedelta(minutes=10))
    with pytest.raises(RequestSkewedError) as excinfo:
        await verifier.verify(uow, stale, now=NOW)
    assert excinfo.value.s3_code == "RequestTimeTooSkewed"


@pytest.mark.asyncio
async def test_future_dated_request_is_refused() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider, clock_skew_seconds=300)

    future = _signed_request(signed_at=NOW + timedelta(minutes=10))
    with pytest.raises(RequestSkewedError):
        await verifier.verify(uow, future, now=NOW)


@pytest.mark.asyncio
async def test_body_not_matching_signed_content_hash_is_refused() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    # Signature is valid over content_sha256, but the delivered body differs.
    request = _signed_request(signed_at=NOW, body=b"signed-body", body_sha256="b" * 64)
    with pytest.raises(ContentHashMismatchError) as excinfo:
        await verifier.verify(uow, request, now=NOW)
    assert excinfo.value.s3_code == "XAmzContentSHA256Mismatch"


@pytest.mark.asyncio
async def test_malformed_authorization_header_is_refused() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    request = _signed_request(signed_at=NOW)
    broken = dict(request.headers)
    broken["authorization"] = "AWS4-HMAC-SHA256 Credential=truncated"
    request = S3Request(
        method=request.method,
        path=request.path,
        query=request.query,
        headers=broken,
        amz_date=request.amz_date,
        content_sha256=request.content_sha256,
        body_sha256=request.body_sha256,
        source_ip=request.source_ip,
    )
    with pytest.raises(SignatureMismatchError):
        await verifier.verify(uow, request, now=NOW)


# --- failure recording and rate limiting -----------------------------------


@pytest.mark.asyncio
async def test_a_failure_is_audited_under_the_s3_protocol() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    with pytest.raises(SignatureMismatchError):
        await verifier.verify(uow, _signed_request(signed_at=NOW, tamper_signature=True), now=NOW)

    record = uow.audit.records[-1]
    assert record.action == AuditAction.AUTH_DENIED
    assert record.protocol == AuditProtocol.S3
    assert record.source_ip == SOURCE_IP
    assert record.reason == "signature_mismatch"


@pytest.mark.asyncio
async def test_repeated_failures_are_rate_limited_per_source_ip() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider, limit=3)

    for _ in range(3):
        with pytest.raises(SignatureMismatchError):
            await verifier.verify(
                uow, _signed_request(signed_at=NOW, tamper_signature=True), now=NOW
            )

    # The fourth attempt is throttled before any signing work runs -- even a
    # now-valid signature from the same IP is refused.
    with pytest.raises(RateLimitedError):
        await verifier.verify(uow, _signed_request(signed_at=NOW), now=NOW)
    assert uow.audit.records[-1].action == AuditAction.AUTH_RATE_LIMITED
    assert uow.audit.records[-1].protocol == AuditProtocol.S3


@pytest.mark.asyncio
async def test_a_request_without_a_source_ip_is_not_rate_limited() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider, limit=1)

    for _ in range(3):
        with pytest.raises(SignatureMismatchError):
            await verifier.verify(
                uow,
                _signed_request(signed_at=NOW, tamper_signature=True, source_ip=None),
                now=NOW,
            )
    # Still refused for the signature, never for rate limiting, since there is
    # no key to count against.
    with pytest.raises(SignatureMismatchError):
        await verifier.verify(
            uow, _signed_request(signed_at=NOW, tamper_signature=True, source_ip=None), now=NOW
        )


# --- presigned URLs (phase 9) ----------------------------------------------

HOST = "s3.cyberfs.local"
BASE_PATH = "/s3"
BUCKET = OWNER
OBJECT_KEY = "reports/q3.xlsx"


def _presigned_request(
    *,
    signed_at: datetime,
    secret: str = SECRET,
    access_key_id: str = ACCESS_KEY_ID,
    expires: int = 900,
    method: str = "GET",
    tamper: bool = False,
    source_ip: str | None = SOURCE_IP,
) -> S3Request:
    """A genuinely presigned request built over the domain's own functions."""
    path = presigned.object_path(BASE_PATH, BUCKET, OBJECT_KEY)
    query = presigned.build_presigned_query(
        method=method,
        path=path,
        host=HOST,
        access_key_id=access_key_id,
        secret=secret,
        region=REGION,
        service=SERVICE,
        amz_date=signed_at.strftime(sigv4.AMZ_DATE_FORMAT),
        expires=expires,
    )
    if tamper:
        # Flip the last signature nibble so verification must fail.
        marker = "X-Amz-Signature="
        head, _, sig = query.rpartition(marker)
        mutated = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        query = f"{head}{marker}{mutated}"
    return S3Request(
        method=method,
        path=path,
        query=query,
        headers={"host": HOST},
        source_ip=source_ip,
    )


@pytest.mark.asyncio
async def test_valid_presigned_request_authenticates() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    resolved = await verifier.verify(uow, _presigned_request(signed_at=NOW), now=NOW)

    assert resolved.key_id == ACCESS_KEY_ID
    assert resolved.owner_subject == OWNER
    # Last-used is stamped on the presigned path exactly as on the header path.
    assert uow.s3_keys.by_key_id[ACCESS_KEY_ID].last_used_at == NOW


@pytest.mark.asyncio
async def test_a_bad_presigned_signature_is_refused() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    with pytest.raises(SignatureMismatchError):
        await verifier.verify(uow, _presigned_request(signed_at=NOW, tamper=True), now=NOW)


@pytest.mark.asyncio
async def test_an_expired_presigned_url_is_access_denied_not_skewed() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    # Signed 15 minutes ago with a 5-minute lifetime: past expiry.
    request = _presigned_request(signed_at=NOW - timedelta(minutes=15), expires=300)
    with pytest.raises(S3AccessDeniedError) as excinfo:
        await verifier.verify(uow, request, now=NOW)
    # Expiry is the URL's own lifetime, reported as AccessDenied -- deliberately
    # NOT RequestTimeTooSkewed, which is a clock-skew failure.
    assert excinfo.value.s3_code == "AccessDenied"
    assert not isinstance(excinfo.value, RequestSkewedError)


@pytest.mark.asyncio
async def test_a_presigned_url_within_its_window_is_accepted() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    # Signed 4 minutes ago with a 5-minute lifetime: still inside the window.
    request = _presigned_request(signed_at=NOW - timedelta(minutes=4), expires=300)
    resolved = await verifier.verify(uow, request, now=NOW)
    assert resolved.key_id == ACCESS_KEY_ID


@pytest.mark.asyncio
async def test_a_revoked_key_stops_a_presigned_url_immediately() -> None:
    provider = CountingKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider, revoked=True))
    verifier = _make_verifier(provider)

    with pytest.raises(UnknownAccessKeyError):
        await verifier.verify(uow, _presigned_request(signed_at=NOW), now=NOW)
    # The revoked path unseals the placeholder once, so it stays
    # timing-indistinguishable from an unknown key or a bad signature.
    assert provider.unseal_calls == 1


@pytest.mark.asyncio
async def test_an_unknown_key_on_a_presigned_url_burns_the_placeholder() -> None:
    provider = CountingKeyProvider()
    uow = FakeUnitOfWork()  # no key seeded
    verifier = _make_verifier(provider)

    with pytest.raises(UnknownAccessKeyError) as excinfo:
        await verifier.verify(
            uow, _presigned_request(signed_at=NOW, access_key_id="AKIANOSUCHKEY0000001"), now=NOW
        )
    assert excinfo.value.s3_code == "InvalidAccessKeyId"
    assert provider.unseal_calls == 1


@pytest.mark.asyncio
async def test_a_query_without_a_presign_signature_is_a_bad_signature() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    # No authorization header and no X-Amz-Signature: falls through to the same
    # SignatureDoesNotMatch a malformed header yields.
    request = S3Request(method="GET", path="/s3/user-1/a.txt", query="", headers={"host": HOST})
    with pytest.raises(SignatureMismatchError):
        await verifier.verify(uow, request, now=NOW)
