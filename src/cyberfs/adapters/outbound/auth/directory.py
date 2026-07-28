"""Recipient lookup against CyberdyneAuth's org directory.

CyberdyneAuth deliberately publishes no global email-to-user lookup -- that
would be an enumeration oracle. What it offers is `GET /orgs/{id}/members`,
gated on the `directory:read` scope, intended for exactly this: service-side
user pickers.

So sharing by email resolves only within organisations the sharer belongs to.
Sharing by subject needs no lookup and works regardless.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import httpx

from cyberfs.adapters.outbound.auth.service_token import ServiceTokenProvider
from cyberfs.domain.errors import DependencyForbiddenError, DependencyUnavailableError
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

MEMBER_PAGE_SIZE = 50


def looks_like_a_subject(identifier: str) -> bool:
    """CyberdyneAuth subjects are the user's UUID, stringified."""
    try:
        uuid.UUID(identifier)
    except ValueError:
        return False
    return True


class CyberdyneDirectory:
    """Implements the `UserDirectory` port."""

    def __init__(
        self,
        base_url: str,
        service_tokens: ServiceTokenProvider,
        http: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_tokens = service_tokens
        self._http = http

    async def find_subject(self, identifier: str, *, within_orgs: Sequence[str] = ()) -> str | None:
        candidate = identifier.strip()
        if not candidate:
            return None
        if looks_like_a_subject(candidate):
            # Already a subject; no lookup, and no enumeration surface.
            return candidate
        return await self._search_orgs(candidate, within_orgs)

    async def _search_orgs(self, email: str, orgs: Sequence[str]) -> str | None:
        if not orgs:
            return None
        token = await self._service_tokens.token()
        for org_id in orgs:
            found = await self._search_one(org_id, email, token)
            if found is not None:
                return found
        return None

    async def _search_one(self, org_id: str, email: str, token: str) -> str | None:
        try:
            response = await self._http.get(
                f"{self._base_url}/api/v1/orgs/{org_id}/members",
                params={"search": email, "page_size": MEMBER_PAGE_SIZE, "is_active": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                # The directory answered and refused *us*. Reporting this as an
                # outage cost real diagnosis time once already: CyberFS's OAuth
                # client was registered without `directory:read`, every layer
                # said "unavailable", and the apparent fix was to wait.
                logger.error(
                    "directory_lookup_forbidden",
                    status=response.status_code,
                    detail=response.text[:200],
                    hint="grant this deployment's OAuth client the 'directory:read' scope",
                )
                raise DependencyForbiddenError(
                    "CyberFS is not permitted to read the user directory; its OAuth "
                    "client needs the 'directory:read' scope"
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("directory_lookup_failed", error=type(exc).__name__)
            raise DependencyUnavailableError("the user directory is unavailable") from exc

        wanted = email.casefold()
        for member in response.json().get("members", []):
            # `search` is a substring match, so the exact address is confirmed
            # here -- a share must never land on a near-miss.
            if str(member.get("email") or "").casefold() == wanted:
                subject = member.get("id")
                return str(subject) if subject else None
        return None
