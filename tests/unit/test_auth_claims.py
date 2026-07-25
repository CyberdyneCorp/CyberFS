"""Claim interpretation -- `authentication/spec.md`, principal resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cyberfs.domain.auth.claims import principal_from_claims
from cyberfs.domain.errors import InvalidTokenError

ORG_A = {"id": "org-a", "short_name": "alpha", "github_login": "alpha-inc"}
ORG_B = {"id": "org-b", "short_name": "beta"}


def claims(**overrides: Any) -> dict[str, Any]:
    return {"sub": "user-1", "exp": 1893456000, **overrides}


def test_subject_is_taken_from_sub() -> None:
    assert principal_from_claims(claims()).subject == "user-1"


def test_token_without_sub_or_client_id_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        principal_from_claims({"exp": 1893456000})


def test_admin_flag_requires_literal_true() -> None:
    assert principal_from_claims(claims(is_admin=True)).is_admin
    assert not principal_from_claims(claims(is_admin="true")).is_admin
    assert not principal_from_claims(claims(is_admin=1)).is_admin
    assert not principal_from_claims(claims()).is_admin


def test_expiry_is_parsed_as_utc() -> None:
    principal = principal_from_claims(claims(exp=1893456000))
    assert principal.expires_at == datetime.fromtimestamp(1893456000, tz=UTC)


def test_missing_expiry_is_none() -> None:
    assert principal_from_claims({"sub": "user-1"}).expires_at is None


# --- orgs ------------------------------------------------------------------


def test_orgs_are_parsed() -> None:
    principal = principal_from_claims(claims(orgs=[ORG_A, ORG_B]))
    assert principal.authorized_org_ids == {"org-a", "org-b"}
    assert principal.orgs_claim_present


def test_primary_org_is_parsed() -> None:
    principal = principal_from_claims(claims(org=ORG_A))
    assert principal.org is not None
    assert principal.org.short_name == "alpha"
    assert principal.org.github_login == "alpha-inc"


def test_missing_orgs_claim_is_no_access_not_all_access() -> None:
    """CyberdyneAuth's contract: absence is a legacy token, never a wildcard."""
    principal = principal_from_claims(claims())
    assert principal.authorized_org_ids == frozenset()
    assert not principal.orgs_claim_present


def test_empty_orgs_claim_is_distinguishable_from_absent() -> None:
    principal = principal_from_claims(claims(orgs=[]))
    assert principal.authorized_org_ids == frozenset()
    assert principal.orgs_claim_present


@pytest.mark.parametrize("bad", ["not-a-list", 42, {"id": "x"}, None])
def test_malformed_orgs_claim_yields_no_access(bad: Any) -> None:
    assert principal_from_claims(claims(orgs=bad)).authorized_org_ids == frozenset()


def test_org_entries_without_an_id_are_dropped() -> None:
    principal = principal_from_claims(claims(orgs=[ORG_A, {"short_name": "nameless"}, "junk"]))
    assert principal.authorized_org_ids == {"org-a"}


def test_malformed_primary_org_is_none() -> None:
    assert principal_from_claims(claims(org="alpha")).org is None
    assert principal_from_claims(claims(org={"short_name": "no-id"})).org is None


# --- entitlements ----------------------------------------------------------


def test_entitlements_are_parsed() -> None:
    principal = principal_from_claims(claims(entitlements=["cyberfs", "cyberfs:pro"]))
    assert principal.entitlements == ("cyberfs", "cyberfs:pro")


def test_non_string_entitlements_are_dropped() -> None:
    assert principal_from_claims(claims(entitlements=["ok", 7, None])).entitlements == ("ok",)


# --- service principals ----------------------------------------------------


def test_client_credentials_token_is_a_service_principal() -> None:
    """The real CyberdyneAuth shape: type `service`, subject `client:<id>`."""
    principal = principal_from_claims(
        {
            "sub": "client:reporting-svc",
            "client_id": "reporting-svc",
            "type": "service",
            "scope": "openid profile",
            "exp": 1893456000,
        }
    )
    assert principal.is_service
    assert not principal.is_user
    assert principal.subject == "reporting-svc"


def test_token_with_a_client_id_and_no_subject_is_a_service_principal() -> None:
    """Defensive: a token with no subject at all cannot belong to a user."""
    principal = principal_from_claims({"client_id": "reporting-svc", "exp": 1893456000})
    assert principal.is_service
    assert principal.subject == "reporting-svc"


def test_user_token_is_not_a_service_principal() -> None:
    principal = principal_from_claims(claims())
    assert not principal.is_service
    assert principal.is_user


def test_user_acting_through_a_client_is_still_a_user() -> None:
    """A user token issued to a client app has sub != client_id."""
    principal = principal_from_claims(claims(client_id="web-app"))
    assert not principal.is_service
    assert principal.subject == "user-1"


# --- stability -------------------------------------------------------------


def test_identity_is_stable_across_an_email_change() -> None:
    """`sub` is the identity; email is not part of the principal at all."""
    before = principal_from_claims(claims(email="old@example.test"))
    after = principal_from_claims(claims(email="new@example.test"))
    assert before.subject == after.subject


# --- token type (regression: CyberdyneAuth signs every token kind alike) ----


def test_access_token_is_accepted() -> None:
    assert principal_from_claims(claims(type="access")).subject == "user-1"


def test_refresh_token_is_not_a_bearer_credential() -> None:
    """A refresh token carries the same issuer and signature as an access token."""
    with pytest.raises(InvalidTokenError, match="not an access token"):
        principal_from_claims(claims(type="refresh", sid="session-1"))


def test_mfa_challenge_token_is_rejected() -> None:
    """Issued after the password step but BEFORE the second factor is verified.

    Accepting one would let a caller who has only completed half of two-factor
    authentication act as the fully authenticated user.
    """
    with pytest.raises(InvalidTokenError, match="not an access token"):
        principal_from_claims(claims(type="mfa"))


@pytest.mark.parametrize("bad", ["id", "unknown", "", 42, None])
def test_unrecognised_token_type_is_rejected(bad: Any) -> None:
    if bad is None:
        pytest.skip("absent type is covered separately")
    with pytest.raises(InvalidTokenError):
        principal_from_claims(claims(type=bad))


def test_token_without_a_type_claim_is_tolerated() -> None:
    """A legacy or non-CyberdyneAuth token is still fully validated otherwise."""
    assert principal_from_claims(claims()).subject == "user-1"


# --- service subjects (regression: sub is `client:<id>`, not `<id>`) --------


def test_service_token_subject_drops_the_client_prefix() -> None:
    principal = principal_from_claims(
        {
            "sub": "client:cyberfs",
            "client_id": "cyberfs",
            "type": "service",
            "exp": 1893456000,
        }
    )
    assert principal.is_service
    assert principal.subject == "cyberfs"


def test_service_token_is_detected_from_the_type_claim() -> None:
    principal = principal_from_claims(
        {"sub": "client:reporting", "type": "service", "exp": 1893456000}
    )
    assert principal.is_service
    assert principal.subject == "reporting"


def test_service_principal_cannot_be_an_admin() -> None:
    """A service has no user identity, so an is_admin claim on one means nothing."""
    principal = principal_from_claims(
        {
            "sub": "client:cyberfs",
            "client_id": "cyberfs",
            "type": "service",
            "is_admin": True,
            "exp": 1893456000,
        }
    )
    assert principal.is_service
    assert not principal.is_admin


def test_user_token_with_admin_is_still_admin() -> None:
    assert principal_from_claims(claims(type="access", is_admin=True)).is_admin


# --- introspection payloads (RFC 7662 uses `token_type`, not `type`) --------


def test_introspected_user_token_is_a_user_principal() -> None:
    """Verified against a live instance: introspection says token_type=user."""
    principal = principal_from_claims(
        {
            "active": True,
            "sub": "user-1",
            "token_type": "user",
            "is_admin": True,
            "exp": 1893456000,
        }
    )
    assert not principal.is_service
    assert principal.is_admin
    assert principal.subject == "user-1"


def test_introspected_service_token_is_a_service_principal() -> None:
    """The exact payload a live CyberdyneAuth returned for the cyberfs client."""
    principal = principal_from_claims(
        {
            "active": True,
            "sub": "client:cyb_YpkLeby8cpqxGKtp",
            "scope": "directory:read openid",
            "client_id": "cyb_YpkLeby8cpqxGKtp",
            "token_type": "service",
            "exp": 1893456000,
        }
    )
    assert principal.is_service
    assert principal.subject == "cyb_YpkLeby8cpqxGKtp"
    assert not principal.is_admin


@pytest.mark.parametrize("bad", ["refresh", "mfa", "unknown"])
def test_unexpected_introspection_token_type_is_rejected(bad: str) -> None:
    with pytest.raises(InvalidTokenError, match="not an access token"):
        principal_from_claims({"active": True, "sub": "u", "token_type": bad, "exp": 1})


def test_jwt_type_wins_over_introspection_token_type() -> None:
    """A JWT `type` is authoritative when both spellings somehow appear."""
    with pytest.raises(InvalidTokenError):
        principal_from_claims(
            {"sub": "u", "type": "refresh", "token_type": "user", "exp": 1893456000}
        )
