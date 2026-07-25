"""Client-credentials service token for calling CyberdyneAuth.

Introspection requires CyberFS to present its own service token. The token is
cached until shortly before it expires so a stampede of introspections does
not become a stampede of token requests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx

from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import DependencyUnavailableError
from cyberfs.infrastructure.logging import get_logger

#: Renew this far ahead of expiry, so a token never expires mid-flight.
RENEW_MARGIN = timedelta(seconds=60)

logger = get_logger(__name__)


class ServiceTokenProvider:
    def __init__(
        self,
        discovery: DiscoveryClient,
        http: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._discovery = discovery
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._renew_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def token(self, now: datetime | None = None) -> str:
        moment = now or utcnow()
        if self._token is not None and self._renew_at is not None and moment < self._renew_at:
            return self._token
        async with self._lock:
            if self._token is not None and self._renew_at is not None and moment < self._renew_at:
                return self._token
            return await self._request(moment)

    async def _request(self, now: datetime) -> str:
        metadata = await self._discovery.metadata(now)
        if metadata.token_endpoint is None:
            raise DependencyUnavailableError("discovery advertises no token endpoint")
        try:
            response = await self._http.post(
                metadata.token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("service_token_request_failed", error=type(exc).__name__)
            raise DependencyUnavailableError("could not obtain a service token") from exc

        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise DependencyUnavailableError("token endpoint returned no access_token")

        expires_in = payload.get("expires_in")
        lifetime = (
            timedelta(seconds=int(expires_in)) if isinstance(expires_in, int) else RENEW_MARGIN
        )
        self._token = token
        self._renew_at = now + max(lifetime - RENEW_MARGIN, timedelta(0))
        return token

    def invalidate(self) -> None:
        """Drop the cached token, e.g. after CyberdyneAuth rejects it."""
        self._token = None
        self._renew_at = None
