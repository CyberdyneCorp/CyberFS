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


def principal_from_claims(claims: Mapping[str, Any]) -> Principal:
    """Build a principal from verified claims.

    A client-credentials token is recognised by carrying `client_id` with
    either no `sub` or a `sub` equal to it -- the standard shape for a token
    issued to a service rather than to a person.
    """
    subject = claims.get("sub")
    client_id = claims.get("client_id")
    subject = subject if isinstance(subject, str) and subject else None
    client_id = client_id if isinstance(client_id, str) and client_id else None

    is_service = client_id is not None and (subject is None or subject == client_id)
    resolved = subject or client_id
    if resolved is None:
        raise InvalidTokenError("token carries neither sub nor client_id")

    return Principal(
        subject=resolved,
        is_admin=claims.get("is_admin") is True,
        is_service=is_service,
        org=_org_from(claims.get("org")),
        orgs=_orgs_from(claims.get("orgs")),
        orgs_claim_present="orgs" in claims,
        entitlements=_strings_from(claims.get("entitlements")),
        expires_at=_expires_at(claims.get("exp")),
    )
