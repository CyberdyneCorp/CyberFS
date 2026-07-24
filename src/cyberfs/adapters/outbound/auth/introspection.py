"""RFC 7662 token introspection.

Used where the JWT claim is not fresh enough: admin actions, grants,
revocations, and ownership transfer. If CyberdyneAuth cannot be reached, this
fails closed with a dependency error -- falling back to the local claim would
defeat the entire point of asking.
"""

from __future__ import annotations

from typing import Any

import httpx

from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient
from cyberfs.adapters.outbound.auth.service_token import ServiceTokenProvider
from cyberfs.domain.auth.claims import principal_from_claims
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import DependencyUnavailableError, InvalidTokenError
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)


class TokenIntrospectionClient:
    """Implements the `TokenIntrospector` port."""

    def __init__(
        self,
        discovery: DiscoveryClient,
        service_tokens: ServiceTokenProvider,
        http: httpx.AsyncClient,
    ) -> None:
        self._discovery = discovery
        self._service_tokens = service_tokens
        self._http = http

    async def introspect(self, token: str) -> Principal:
        payload = await self._call(token)
        if payload.get("active") is not True:
            raise InvalidTokenError("token is not active")
        return principal_from_claims(payload)

    async def _call(self, token: str) -> dict[str, Any]:
        metadata = await self._discovery.metadata()
        if metadata.introspection_endpoint is None:
            raise DependencyUnavailableError("discovery advertises no introspection endpoint")

        service_token = await self._service_tokens.token()
        try:
            response = await self._post(metadata.introspection_endpoint, token, service_token)
            if response.status_code == httpx.codes.UNAUTHORIZED:
                # Our own service token went stale; retry once with a fresh one.
                self._service_tokens.invalidate()
                response = await self._post(
                    metadata.introspection_endpoint,
                    token,
                    await self._service_tokens.token(),
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("introspection_unavailable", error=type(exc).__name__)
            raise DependencyUnavailableError("token introspection is unavailable") from exc

        result: dict[str, Any] = response.json()
        return result

    async def _post(self, endpoint: str, token: str, service_token: str) -> httpx.Response:
        return await self._http.post(
            endpoint,
            data={"token": token, "token_type_hint": "access_token"},
            headers={
                "Authorization": f"Bearer {service_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
