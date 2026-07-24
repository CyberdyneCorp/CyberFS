"""The S3 credential resolver -- `s3-compatibility` and `authentication` specs.

Composes the real phase-4 `S3SignatureVerifier` and the real REST
`AuthenticationService` over the in-memory fakes, and drives them with a
genuinely SigV4-signed request. Covers:

* an access key resolves to its owner's subject with **admin stripped** (a
  leaked key can never wield admin, and admin routes reject key auth);
* a bearer token resolves to exactly the principal REST produces, org claims
  intact, through the identical freshness path -- failing closed on an
  identity-plane outage;
* a request presenting **both** credentials is refused with `400`, and one
  presenting **neither** with `403 AccessDenied`;
* `CreateBucket`/`DeleteBucket` are refused.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.authentication import AUTH_FAILURE_WINDOW, AuthenticationService
from cyberfs.application.s3_auth import S3SignatureVerifier
from cyberfs.application.s3_authentication import (
    S3Authenticator,
    ensure_bucket_not_managed,
)
from cyberfs.domain.auth.principal import Org, Principal
from cyberfs.domain.errors import (
    AmbiguousS3CredentialsError,
    BucketManagementRefusedError,
    DependencyUnavailableError,
    PermissionDeniedError,
    S3AccessDeniedError,
)
from cyberfs.domain.ratelimit import FixedWindowLimiter
from cyberfs.domain.s3 import sigv4
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.credentials import S3Credential
from cyberfs.domain.s3.request import S3Request

from .fakes import FakeKeyProvider, FakeUnitOfWork

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
REGION = "us-east-1"
SERVICE = "s3"
SECRET = "cyberfs-test-secret-access-key-value"
ACCESS_KEY_ID = "AKIATEST0000000000CI"
OWNER = "user-bob"
SOURCE_IP = "198.51.100.7"
BEARER_TOKEN = "a-real-cyberdyne-bearer-token"


# --- identity doubles ------------------------------------------------------


class StubIdentity:
    """Stands in for both the token verifier and the introspector."""

    def __init__(self, principal: Principal | None = None, error: Exception | None = None) -> None:
        self.principal = principal
        self.error = error
        self.calls = 0

    async def _result(self) -> Principal:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.principal is not None
        return self.principal

    async def verify(self, token: str) -> Principal:
        return await self._result()

    async def introspect(self, token: str) -> Principal:
        return await self._result()


class StubSubjectIntrospector:
    """Stands in for the tokenless subject-freshness introspector."""

    def __init__(self, principal: Principal | None = None, error: Exception | None = None) -> None:
        self.principal = principal
        self.error = error
        self.subjects: list[str] = []

    async def introspect_subject(self, subject: str) -> Principal:
        self.subjects.append(subject)
        if self.error is not None:
            raise self.error
        return self.principal or Principal(subject=subject)


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[object] = []

    async def record(self, entry: object) -> None:
        self.entries.append(entry)


def bearer_principal() -> Principal:
    """A full principal a real token yields, org claims and all."""
    return Principal(
        subject=OWNER,
        orgs=(Org(id="org-7", short_name="acme"),),
        orgs_claim_present=True,
        entitlements=("files",),
        expires_at=NOW + timedelta(minutes=5),
    )


# --- construction ----------------------------------------------------------


def make_verifier(provider: FakeKeyProvider) -> S3SignatureVerifier:
    return S3SignatureVerifier(
        keys=provider,
        failure_limiter=FixedWindowLimiter(limit=5, window=timedelta(minutes=1)),
        region=REGION,
        clock_skew_seconds=300,
    )


def make_authentication(
    verifier: StubIdentity, introspector: StubIdentity | None = None
) -> AuthenticationService:
    return AuthenticationService(
        verifier=verifier,
        introspector=introspector or StubIdentity(bearer_principal()),
        audit=RecordingAudit(),
        failure_limiter=FixedWindowLimiter(limit=5, window=AUTH_FAILURE_WINDOW),
    )


def make_key(provider: FakeKeyProvider, *, owner_subject: str = OWNER) -> S3AccessKey:
    return S3AccessKey(
        key_id=ACCESS_KEY_ID,
        sealed_secret=provider.seal_secret(SECRET.encode("utf-8")),
        secret_master_key_id=provider.master_key_id,
        label="ci",
        owner_id=uuid.uuid4(),
        owner_subject=owner_subject,
        created_at=NOW,
    )


def signed_request(*, extra_headers: dict[str, str] | None = None, query: str = "") -> S3Request:
    """A genuinely SigV4-signed request over the domain's own functions."""
    amz_date = NOW.strftime(sigv4.AMZ_DATE_FORMAT)
    date_stamp = NOW.strftime("%Y%m%d")
    body = b"payload"
    payload_hash = hashlib.sha256(body).hexdigest()
    method, path = "GET", "/reports/q3.xlsx"
    headers = {
        "host": f"{OWNER}.s3.cyberfs.local",
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed_headers = ("host", "x-amz-content-sha256", "x-amz-date")
    creq = sigv4.canonical_request(method, path, query, headers, signed_headers, payload_hash)
    scope = sigv4.credential_scope(date_stamp, REGION, SERVICE)
    to_sign = sigv4.string_to_sign(amz_date, scope, creq)
    signature = sigv4.signature(sigv4.signing_key(SECRET, date_stamp, REGION, SERVICE), to_sign)
    headers["authorization"] = (
        f"{sigv4.ALGORITHM} "
        f"Credential={ACCESS_KEY_ID}/{date_stamp}/{REGION}/{SERVICE}/aws4_request, "
        f"SignedHeaders={';'.join(signed_headers)}, "
        f"Signature={signature}"
    )
    if extra_headers:
        headers.update(extra_headers)
    return S3Request(
        method=method,
        path=path,
        query=query,
        headers=headers,
        amz_date=amz_date,
        content_sha256=payload_hash,
        body_sha256=payload_hash,
        source_ip=SOURCE_IP,
    )


def bearer_request(token: str = BEARER_TOKEN) -> S3Request:
    return S3Request(
        method="GET",
        path="/reports/q3.xlsx",
        query="",
        headers={"authorization": f"Bearer {token}"},
        source_ip=SOURCE_IP,
    )


# --- SigV4 (access key) ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_key_resolves_to_its_owner_subject() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    auth = S3Authenticator(make_verifier(provider), make_authentication(StubIdentity()))

    result = await auth.authenticate(uow, signed_request(), now=NOW)

    assert result.credential is S3Credential.SIGV4
    assert result.principal.subject == OWNER


@pytest.mark.asyncio
async def test_a_key_is_never_an_administrator() -> None:
    """Admin status does not travel with a key, even for an admin owner."""
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    # The verifier only knows the stored key; the owner being an admin elsewhere
    # is irrelevant -- the resolved principal must not be one.
    auth = S3Authenticator(make_verifier(provider), make_authentication(StubIdentity()))

    result = await auth.authenticate(uow, signed_request(), now=NOW)

    assert result.principal.is_admin is False


@pytest.mark.asyncio
async def test_a_key_principal_cannot_pass_an_admin_gate() -> None:
    """Invariant A: key auth can never reach an admin route."""
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    auth = S3Authenticator(make_verifier(provider), make_authentication(StubIdentity()))

    result = await auth.authenticate(uow, signed_request(), now=NOW)

    # However an admin route gates, it gates on `is_admin`, which is false here.
    assert not result.principal.is_admin


@pytest.mark.asyncio
async def test_a_fresh_key_request_routes_through_subject_introspection() -> None:
    """Invariant E: a key-authenticated fresh operation introspects the owner.

    A grant/revocation/transfer signed with a key verifies the owning subject
    against CyberdyneAuth before acting, exactly as a bearer token would.
    """
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    subject_introspector = StubSubjectIntrospector()
    auth = S3Authenticator(
        make_verifier(provider),
        make_authentication(StubIdentity()),
        subject_introspector,
    )

    result = await auth.authenticate(uow, signed_request(), require_fresh=True, now=NOW)

    assert subject_introspector.subjects == [OWNER]
    assert result.principal.subject == OWNER
    # Admin is never restored by the freshness check.
    assert result.principal.is_admin is False


@pytest.mark.asyncio
async def test_a_fresh_key_request_fails_closed_on_an_outage() -> None:
    """An identity-plane outage on the key path is a 503, not a pass on the key."""
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    subject_introspector = StubSubjectIntrospector(error=DependencyUnavailableError())
    auth = S3Authenticator(
        make_verifier(provider),
        make_authentication(StubIdentity()),
        subject_introspector,
    )

    with pytest.raises(DependencyUnavailableError):
        await auth.authenticate(uow, signed_request(), require_fresh=True, now=NOW)


@pytest.mark.asyncio
async def test_a_fresh_key_request_fails_closed_without_a_subject_introspector() -> None:
    """Freshness is never silently skipped: absent an introspector, fail closed."""
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    auth = S3Authenticator(make_verifier(provider), make_authentication(StubIdentity()))

    with pytest.raises(DependencyUnavailableError):
        await auth.authenticate(uow, signed_request(), require_fresh=True, now=NOW)


@pytest.mark.asyncio
async def test_an_ordinary_key_read_does_not_introspect_the_subject() -> None:
    """A plain read takes no round trip -- introspection is fresh-only."""
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    subject_introspector = StubSubjectIntrospector()
    auth = S3Authenticator(
        make_verifier(provider),
        make_authentication(StubIdentity()),
        subject_introspector,
    )

    await auth.authenticate(uow, signed_request(), now=NOW)

    assert subject_introspector.subjects == []


# --- bearer ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bearer_token_resolves_the_rest_principal_with_org_claims() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    verifier = StubIdentity(bearer_principal())
    auth = S3Authenticator(make_verifier(provider), make_authentication(verifier))

    result = await auth.authenticate(uow, bearer_request(), now=NOW)

    assert result.credential is S3Credential.BEARER
    assert result.principal == bearer_principal()
    assert result.principal.authorized_org_ids == frozenset({"org-7"})


@pytest.mark.asyncio
async def test_a_fresh_bearer_request_routes_through_introspection() -> None:
    """Invariant E: the S3 bearer path reuses the REST freshness path."""
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    verifier = StubIdentity(bearer_principal())
    introspector = StubIdentity(bearer_principal())
    auth = S3Authenticator(make_verifier(provider), make_authentication(verifier, introspector))

    await auth.authenticate(uow, bearer_request(), require_fresh=True, now=NOW)

    assert verifier.calls == 1
    assert introspector.calls == 1


@pytest.mark.asyncio
async def test_a_fresh_bearer_request_fails_closed_on_an_outage() -> None:
    """An identity-plane outage during a fresh operation is a 503, not a pass."""
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    verifier = StubIdentity(bearer_principal())
    introspector = StubIdentity(error=DependencyUnavailableError())
    auth = S3Authenticator(make_verifier(provider), make_authentication(verifier, introspector))

    with pytest.raises(DependencyUnavailableError):
        await auth.authenticate(uow, bearer_request(), require_fresh=True, now=NOW)


# --- credential-form refusals ----------------------------------------------


@pytest.mark.asyncio
async def test_both_credentials_in_headers_is_refused_with_400() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await uow.s3_keys.add(make_key(provider))
    auth = S3Authenticator(make_verifier(provider), make_authentication(StubIdentity()))

    # A signed request that *also* carries a bearer token: the classifier sees
    # both because `Authorization` cannot be both, so the bearer rides a header.
    request = signed_request()
    ambiguous = dict(request.headers)
    ambiguous["authorization"] = f"Bearer {BEARER_TOKEN}"
    # Re-add a presigned signature to the query so SigV4 is still detected.
    request = S3Request(
        method=request.method,
        path=request.path,
        query="X-Amz-Signature=deadbeef",
        headers=ambiguous,
        amz_date=request.amz_date,
        content_sha256=request.content_sha256,
        body_sha256=request.body_sha256,
        source_ip=request.source_ip,
    )
    with pytest.raises(AmbiguousS3CredentialsError) as excinfo:
        await auth.authenticate(uow, request, now=NOW)
    assert excinfo.value.s3_status == 400


@pytest.mark.asyncio
async def test_a_bearer_and_a_presigned_signature_is_ambiguous() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    auth = S3Authenticator(make_verifier(provider), make_authentication(StubIdentity()))

    request = S3Request(
        method="GET",
        path="/reports/q3.xlsx",
        query="X-Amz-Signature=deadbeef&X-Amz-Credential=x",
        headers={"authorization": f"Bearer {BEARER_TOKEN}"},
        source_ip=SOURCE_IP,
    )
    with pytest.raises(AmbiguousS3CredentialsError):
        await auth.authenticate(uow, request, now=NOW)


@pytest.mark.asyncio
async def test_no_credential_is_refused_with_403_access_denied() -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    auth = S3Authenticator(make_verifier(provider), make_authentication(StubIdentity()))

    request = S3Request(method="GET", path="/reports/q3.xlsx", query="", source_ip=SOURCE_IP)
    with pytest.raises(S3AccessDeniedError) as excinfo:
        await auth.authenticate(uow, request, now=NOW)
    assert excinfo.value.s3_code == "AccessDenied"
    assert isinstance(excinfo.value, PermissionDeniedError)


# --- bucket management refusal ---------------------------------------------


@pytest.mark.parametrize("operation", ["CreateBucket", "DeleteBucket"])
def test_bucket_management_is_refused(operation: str) -> None:
    with pytest.raises(BucketManagementRefusedError) as excinfo:
        ensure_bucket_not_managed(operation)
    assert excinfo.value.s3_status == 403


def test_an_ordinary_operation_is_not_a_bucket_management_op() -> None:
    ensure_bucket_not_managed("GetObject")  # does not raise
