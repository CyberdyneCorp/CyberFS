"""The authenticated caller.

CyberFS identifies every caller by the CyberdyneAuth `sub` claim. Ownership,
grants, and quota all hang off that identifier, so it must stay stable across
email changes and login-method changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Org:
    id: str
    short_name: str
    github_login: str | None = None


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified caller.

    `orgs_claim_present` distinguishes a token that said "no orgs" from a
    legacy token that said nothing. Neither grants org-scoped access -- see
    `authorized_org_ids` -- but the distinction matters for diagnosing why a
    caller sees nothing.
    """

    subject: str
    is_admin: bool = False
    is_service: bool = False
    org: Org | None = None
    orgs: tuple[Org, ...] = ()
    orgs_claim_present: bool = False
    entitlements: tuple[str, ...] = ()
    expires_at: datetime | None = None

    @property
    def authorized_org_ids(self) -> frozenset[str]:
        """Orgs this caller may act in.

        A missing `orgs` claim yields the empty set, never "all orgs" --
        CyberdyneAuth's token contract is explicit that absence is a legacy
        token, not a wildcard.
        """
        return frozenset(org.id for org in self.orgs)

    @property
    def is_user(self) -> bool:
        """Whether this principal can own files.

        Service principals act on their own behalf and have no home tree, so
        they can never be a node owner or a share recipient.
        """
        return not self.is_service
