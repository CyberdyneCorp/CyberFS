"""Resolving an S3 caller to a `Principal`.

The one entry the S3 surface authenticates through. It composes two pieces that
already exist:

* the phase-4 `S3SignatureVerifier`, which validates a SigV4 signature and
  returns the owning access key; and
* the REST `AuthenticationService`, which verifies a bearer token and -- when
  freshness matters -- reruns it through introspection, failing closed on an
  identity-plane outage.

Its obligations, each pinned by the spec:

* a request carrying *both* a signature and a bearer token is refused with
  ``400`` -- the server never silently picks one
  (``s3-compatibility/spec.md``);
* a request carrying *neither* is ``403 AccessDenied``;
* a bearer token resolves to exactly the principal REST would produce, org
  claims and all, through the identical freshness path
  (``authentication/spec.md``, "Freshness rules still apply");
* an access key resolves to its owner's subject with **administrator status
  stripped** -- a long-lived key is the credential most likely to leak, and
  administrative actions require a freshly introspected token, so key auth can
  never be an administrator and is refused on admin routes.

Design note on org claims (task 10.7). A bearer token yields a full principal
with its org claims, unchanged from REST. An access key yields
``Principal(subject=owner_subject)`` with no org claims, because a key carries
no token and CyberdyneAuth exposes no subject->claims lookup here. This is
safe: file authorization hangs off subject, ownership, and grants, never off
``authorized_org_ids`` -- org claims only gate the interactive, bearer-only,
freshness-gated share *directory*, which no S3 operation reaches.

Freshness on the key path. A key-authenticated grant, revocation, or ownership
transfer (`require_fresh`) reflects the identity plane *now*, exactly as a
bearer token does (`authentication/spec.md`, "Key-authenticated grants still
introspect"). Because a key carries no token, freshness rides a
``SubjectIntrospector`` keyed by the owning subject: a deactivated owner is
refused, and an identity-plane outage fails the operation closed (``503``)
rather than proceeding on the strength of the key alone. Ordinary reads take no
round trip. Admin status is never restored by this check -- a key stays
non-administrative regardless of what the identity plane reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cyberfs.application.authentication import AuthenticationService
from cyberfs.application.s3_auth import S3SignatureVerifier
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    AmbiguousS3CredentialsError,
    BucketManagementRefusedError,
    DependencyUnavailableError,
    S3AccessDeniedError,
)
from cyberfs.domain.ports.identity import SubjectIntrospector
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.credentials import (
    S3Credential,
    S3CredentialForm,
    bearer_token,
    classify_credentials,
)
from cyberfs.domain.s3.request import S3Request

#: Operations a client may not perform: a bucket corresponds to a user and is
#: not client-managed (`s3-compatibility/spec.md`).
_MANAGED_BUCKET_OPS = frozenset({"CreateBucket", "DeleteBucket"})


@dataclass(frozen=True, slots=True)
class S3Authenticated:
    """The outcome of authenticating an S3 request.

    `credential` records how the caller proved themselves, so a downstream
    admin guard can refuse a SigV4-authenticated caller without re-deriving it.
    """

    principal: Principal
    credential: S3Credential


class S3Authenticator:
    def __init__(
        self,
        verifier: S3SignatureVerifier,
        auth: AuthenticationService,
        subject_introspector: SubjectIntrospector | None = None,
    ) -> None:
        self._verifier = verifier
        self._auth = auth
        # Freshness for the tokenless SigV4 path. Absent, a `require_fresh`
        # key request fails closed rather than proceeding on the key alone --
        # freshness is never silently skipped.
        self._subject_introspector = subject_introspector

    async def authenticate(
        self,
        uow: UnitOfWork,
        request: S3Request,
        *,
        require_fresh: bool = False,
        now: datetime | None = None,
    ) -> S3Authenticated:
        """Resolve `request` to an authenticated principal, or raise.

        Raises `AmbiguousS3CredentialsError` (400) when both credentials are
        present, `S3AccessDeniedError` (403) when neither is, and whatever the
        underlying verifier or authentication service raises otherwise.
        """
        form = classify_credentials(request.headers, request.query)
        if form is S3CredentialForm.BOTH:
            raise AmbiguousS3CredentialsError("present one credential, not both")
        if form is S3CredentialForm.NONE:
            raise S3AccessDeniedError("a credential is required")
        if form is S3CredentialForm.BEARER:
            return await self._authenticate_bearer(request, require_fresh, now)
        return await self._authenticate_sigv4(uow, request, require_fresh, now)

    async def _authenticate_bearer(
        self, request: S3Request, require_fresh: bool, now: datetime | None
    ) -> S3Authenticated:
        principal = await self._auth.authenticate(
            bearer_token(request.headers),
            source_ip=request.source_ip,
            require_fresh=require_fresh,
            now=now,
        )
        return S3Authenticated(principal=principal, credential=S3Credential.BEARER)

    async def _authenticate_sigv4(
        self, uow: UnitOfWork, request: S3Request, require_fresh: bool, now: datetime | None
    ) -> S3Authenticated:
        key = await self._verifier.verify(uow, request, now=now)
        if require_fresh:
            await self._ensure_subject_fresh(key.owner_subject)
        return S3Authenticated(
            principal=self._principal_from_key(key), credential=S3Credential.SIGV4
        )

    async def _ensure_subject_fresh(self, subject: str) -> None:
        """Verify the owning subject is still active at CyberdyneAuth.

        Mirrors the bearer freshness path for the tokenless key credential: a
        deactivated owner is refused, and an identity-plane outage -- or the
        absence of a subject introspector -- fails the operation closed, never
        proceeding on the strength of the key alone. The introspection result
        confirms liveness only; admin status is never restored, so a key stays
        non-administrative.
        """
        if self._subject_introspector is None:
            raise DependencyUnavailableError("subject freshness is unavailable")
        await self._subject_introspector.introspect_subject(subject)

    @staticmethod
    def _principal_from_key(key: S3AccessKey) -> Principal:
        """A key resolves to its owner's subject, never an administrator.

        Administrator status is stripped *by construction* -- it is simply never
        set -- so a leaked key can never wield admin, and an admin route (which
        introspects a fresh token) rejects a key-authenticated caller.
        """
        return Principal(subject=key.owner_subject)


def ensure_bucket_not_managed(operation: str) -> None:
    """Refuse `CreateBucket`/`DeleteBucket` -- buckets map to users, not clients."""
    if operation in _MANAGED_BUCKET_OPS:
        raise BucketManagementRefusedError(f"{operation} is not permitted", operation=operation)
