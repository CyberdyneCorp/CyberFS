"""Development stub for running without a live CyberdyneAuth.

`deployment/spec.md` requires the local stack to be usable without the identity
plane, and requires enabling this in production to fail startup. The settings
validator enforces the second half; this module never reaches production
because `create_app` only wires it when `AUTH_DEV_MODE` is set, which the
validator rejects outside local and test.

The stub accepts any token. It reads the caller from the token string so a
developer can act as different users:

    Authorization: Bearer dev:alice
    Authorization: Bearer dev:alice:admin
"""

from __future__ import annotations

from datetime import timedelta

from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.auth.principal import Org, Principal

DEV_PREFIX = "dev:"
DEV_TOKEN_LIFETIME = timedelta(hours=1)
DEFAULT_SUBJECT = "dev-user"
DEV_ORG = Org(id="dev-org", short_name="dev")


def parse_dev_token(token: str) -> Principal:
    """`dev:<subject>[:admin]`, or any other string for the default user."""
    body = token.removeprefix(DEV_PREFIX) if token.startswith(DEV_PREFIX) else ""
    parts = [part for part in body.split(":") if part]
    subject = parts[0] if parts else DEFAULT_SUBJECT
    is_admin = "admin" in parts[1:]
    return Principal(
        subject=subject,
        is_admin=is_admin,
        org=DEV_ORG,
        orgs=(DEV_ORG,),
        orgs_claim_present=True,
        expires_at=utcnow() + DEV_TOKEN_LIFETIME,
    )


class DevModeVerifier:
    """Implements `TokenVerifier` and `TokenIntrospector` for local work."""

    async def verify(self, token: str) -> Principal:
        return parse_dev_token(token)

    async def introspect(self, token: str) -> Principal:
        return parse_dev_token(token)
