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

from collections.abc import Sequence
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


class SubjectIntrospector(Protocol):
    """Verifies a *subject* is still active at CyberdyneAuth, right now.

    The key-authenticated analogue of `TokenIntrospector`. A SigV4 request
    carries no bearer token to introspect, yet a key-authenticated grant,
    revocation, or ownership transfer must still reflect the identity plane
    *now* (`authentication/spec.md`, "Freshness rules still apply" ->
    "Key-authenticated grants still introspect"): a deactivated owner is
    refused exactly as a demoted admin's stale token would be. The subject is
    the freshness key here because that is all a key resolves to.
    """

    async def introspect_subject(self, subject: str) -> Principal:
        """Return the subject as the identity plane sees it *right now*.

        Raises `InvalidTokenError` if the subject is no longer active, and
        `DependencyUnavailableError` if the identity plane cannot be reached --
        never a fallback to the strength of the key alone.
        """
        ...


class UserDirectory(Protocol):
    """Resolves a share recipient to a CyberdyneAuth subject.

    CyberdyneAuth exposes no global email lookup -- only an *org-scoped*
    member directory. Sharing by email therefore works within organisations
    the sharer belongs to, and the caller's orgs are passed in rather than
    assumed. Sharing by subject needs no lookup at all.
    """

    async def find_subject(self, identifier: str, *, within_orgs: Sequence[str] = ()) -> str | None:
        """Resolve a subject or email to a subject id, or None if unknown."""
        ...
