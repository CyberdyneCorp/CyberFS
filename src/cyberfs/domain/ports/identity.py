"""Identity ports.

Two deliberately separate capabilities:

* `TokenVerifier` -- cheap, local, claim-based. Good enough for an ordinary
  read, where an `is_admin` that is at most one access-token lifetime stale is
  acceptable.
* `TokenIntrospector` -- a round trip to CyberdyneAuth, authoritative *now*.
  Required for admin actions, grants, revocations, and ownership transfer,
  where acting on a stale claim would be a security failure.

`authentication/spec.md` draws that line; the split into two ports is what
stops a caller from accidentally using the cheap one where freshness matters.
"""

from __future__ import annotations

from typing import Protocol

from cyberfs.domain.auth.principal import Principal


class TokenVerifier(Protocol):
    """Verifies a bearer token's signature and claims against discovery."""

    async def verify(self, token: str) -> Principal:
        """Return the caller, or raise `AuthenticationError` / subclass."""
        ...


class TokenIntrospector(Protocol):
    """RFC 7662 introspection against CyberdyneAuth."""

    async def introspect(self, token: str) -> Principal:
        """Return the caller as the identity plane sees it *right now*.

        Raises `InvalidTokenError` if the token is inactive, and
        `DependencyUnavailableError` if the identity plane cannot be reached --
        never a fallback to the local claim.
        """
        ...


class UserDirectory(Protocol):
    """Resolves a share recipient to a CyberdyneAuth subject."""

    async def find_subject(self, identifier: str) -> str | None:
        """Resolve a subject or email to a subject id, or None if unknown."""
        ...
