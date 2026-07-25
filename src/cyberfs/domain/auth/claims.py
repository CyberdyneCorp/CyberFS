"""Turn verified JWT claims into a `Principal`.

Signature verification happens in the adapter; by the time claims reach here
they are trusted to be authentic. What remains is interpreting them, and the
interpretation rules are the part worth testing exhaustively.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from cyberfs.domain.auth.principal import Org, Principal
from cyberfs.domain.errors import InvalidTokenError

#: CyberdyneAuth stamps every JWT it issues with a `type`, and signs them all
#: with the same key and issuer: `access` (user), `service` (client
#: credentials), `refresh`, and `mfa` (issued *before* the second factor is
#: verified). Without this check a refresh token -- or worse, a half-completed
#: MFA challenge token -- would verify as a valid access token here.
BEARER_TOKEN_TYPES = frozenset({"access", "service"})

#: RFC 7662 introspection responses describe the same thing under a different
#: key and vocabulary: `token_type` of `user` or `service`. `principal_from_claims`
#: serves both payload shapes, so it understands both spellings.
INTROSPECTION_TOKEN_TYPES = {"user": "access", "service": "service"}

#: Client-credentials subjects are `client:<client_id>`; the prefix is
#: documented as the at-a-glance signal for verifiers.
CLIENT_SUBJECT_PREFIX = "client:"

#: Normalised token kind for a client-credentials caller.
SERVICE_KIND = "service"


def _org_from(raw: Any) -> Org | None:
    if not isinstance(raw, Mapping):
        return None
    org_id = raw.get("id")
    if not isinstance(org_id, str) or not org_id:
        return None
    short_name = raw.get("short_name")
    github_login = raw.get("github_login")
    return Org(
        id=org_id,
        short_name=short_name if isinstance(short_name, str) else "",
        github_login=github_login if isinstance(github_login, str) else None,
    )


def _orgs_from(raw: Any) -> tuple[Org, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    return tuple(org for entry in raw if (org := _org_from(entry)) is not None)


def _strings_from(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    return tuple(entry for entry in raw if isinstance(entry, str))


def _expires_at(raw: Any) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return datetime.fromtimestamp(raw, tz=UTC)


def ensure_bearer_token_type(claims: Mapping[str, Any]) -> str:
    """Reject any CyberdyneAuth token that is not meant to be presented as a bearer.

    Refresh and MFA-challenge tokens carry the same issuer and signature as an
    access token, so signature verification alone does not distinguish them. An
    MFA token is issued after the password step but *before* the second factor
    is checked -- accepting one here would be an authentication bypass.
    """
    raw = claims.get("type")
    if raw is None:
        introspected = claims.get("token_type")
        if introspected is None:
            # Tolerated: a token predating the `type` claim, or a
            # non-CyberdyneAuth issuer. Everything else is still validated.
            return "access"
        if not isinstance(introspected, str) or introspected not in INTROSPECTION_TOKEN_TYPES:
            raise InvalidTokenError("token is not an access token")
        return INTROSPECTION_TOKEN_TYPES[introspected]
    if not isinstance(raw, str) or raw not in BEARER_TOKEN_TYPES:
        raise InvalidTokenError("token is not an access token")
    return raw


def principal_from_claims(claims: Mapping[str, Any]) -> Principal:
    """Build a principal from verified claims.

    A client-credentials token is identified by `type: service`; its subject is
    `client:<client_id>`, which is normalised back to the bare client id so a
    service principal's identity matches the client it was issued to.
    """
    kind = ensure_bearer_token_type(claims)

    raw_subject = claims.get("sub")
    client_id = claims.get("client_id")
    raw_subject = raw_subject if isinstance(raw_subject, str) and raw_subject else None
    client_id = client_id if isinstance(client_id, str) and client_id else None

    # CyberdyneAuth signals a service two ways, both explicit: the `service`
    # token type and the `client:` subject prefix. The third case -- a token
    # with a client id and no subject at all -- cannot be a user by definition.
    is_service = (
        kind == SERVICE_KIND
        or (raw_subject is not None and raw_subject.startswith(CLIENT_SUBJECT_PREFIX))
        or (raw_subject is None and client_id is not None)
    )

    subject = raw_subject
    if is_service:
        stripped = (
            raw_subject.removeprefix(CLIENT_SUBJECT_PREFIX) if raw_subject is not None else None
        )
        subject = client_id or stripped
    if subject is None:
        raise InvalidTokenError("token carries no usable subject")

    return Principal(
        subject=subject,
        # A service principal has no user identity, so it can never be an
        # administrator however the claim is set.
        is_admin=claims.get("is_admin") is True and not is_service,
        is_service=is_service,
        org=_org_from(claims.get("org")),
        orgs=_orgs_from(claims.get("orgs")),
        orgs_claim_present="orgs" in claims,
        entitlements=_strings_from(claims.get("entitlements")),
        expires_at=_expires_at(claims.get("exp")),
    )
