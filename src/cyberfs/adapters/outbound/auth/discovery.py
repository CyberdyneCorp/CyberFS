"""OIDC discovery and JWKS, cached.

CyberdyneAuth's `docs/token-verification.md` is explicit: derive everything you
validate from `/.well-known/openid-configuration`, never hard-code an issuer,
a JWKS URL, or an algorithm. A relying party that hard-codes a value that
merely happens to match today breaks the moment a legitimate fix aligns the
server with its own discovery -- which is what happened in their #47/#114
incident.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx

from cyberfs.domain.auth.policy import CacheWindow, DiscoveryMetadata, may_refresh, utcnow
from cyberfs.domain.errors import DependencyUnavailableError, InvalidTokenError
from cyberfs.infrastructure.logging import get_logger

DISCOVERY_PATH = "/.well-known/openid-configuration"

logger = get_logger(__name__)


def parse_discovery(document: dict[str, Any]) -> DiscoveryMetadata:
    algorithms = document.get("id_token_signing_alg_values_supported") or []
    return DiscoveryMetadata(
        issuer=str(document.get("issuer") or ""),
        jwks_uri=str(document.get("jwks_uri") or ""),
        signing_algorithms=tuple(str(alg) for alg in algorithms),
        introspection_endpoint=_optional_str(document.get("introspection_endpoint")),
        token_endpoint=_optional_str(document.get("token_endpoint")),
    )


def _optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


class _Cached[T]:
    """A fetched document with the time it was fetched."""

    __slots__ = ("fetched_at", "value")

    def __init__(self, value: T, fetched_at: datetime) -> None:
        self.value = value
        self.fetched_at = fetched_at


class DiscoveryClient:
    """Fetches and caches discovery metadata and the JWKS.

    Both documents survive a CyberdyneAuth outage up to their stale window, so
    an auth blip does not take CyberFS down while a usable key set is in hand.
    """

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        discovery_window: CacheWindow,
        jwks_window: CacheWindow,
        refresh_cooldown: timedelta,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._discovery_window = discovery_window
        self._jwks_window = jwks_window
        self._refresh_cooldown = refresh_cooldown

        self._discovery: _Cached[DiscoveryMetadata] | None = None
        self._jwks: _Cached[dict[str, Any]] | None = None
        #: Only kid-triggered refetches, never ordinary TTL fetches.
        self._last_refresh_attempt: datetime | None = None
        # Coalesces concurrent misses so a cold start makes one request, not N.
        self._discovery_lock = asyncio.Lock()
        self._jwks_lock = asyncio.Lock()

    @property
    def discovery_url(self) -> str:
        return f"{self._base_url}{DISCOVERY_PATH}"

    async def metadata(self, now: datetime | None = None) -> DiscoveryMetadata:
        moment = now or utcnow()
        cached = self._discovery
        if cached is not None and self._discovery_window.is_fresh(cached.fetched_at, moment):
            return cached.value

        async with self._discovery_lock:
            cached = self._discovery
            if cached is not None and self._discovery_window.is_fresh(cached.fetched_at, moment):
                return cached.value
            return await self._fetch_discovery(cached, moment)

    async def _fetch_discovery(
        self, cached: _Cached[DiscoveryMetadata] | None, now: datetime
    ) -> DiscoveryMetadata:
        try:
            response = await self._http.get(self.discovery_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return self._fall_back(cached, now, self._discovery_window, "discovery", exc)
        metadata = parse_discovery(response.json())
        self._discovery = _Cached(metadata, now)
        return metadata

    async def jwks(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or utcnow()
        cached = self._jwks
        if cached is not None and self._jwks_window.is_fresh(cached.fetched_at, moment):
            return cached.value

        async with self._jwks_lock:
            cached = self._jwks
            if cached is not None and self._jwks_window.is_fresh(cached.fetched_at, moment):
                return cached.value
            return await self._fetch_jwks(cached, moment)

    async def refresh_jwks_for_unknown_kid(self, now: datetime | None = None) -> dict[str, Any]:
        """Refetch the key set because a token named a `kid` we do not hold.

        Bounded by a cooldown: a flood of tokens signed by a key that genuinely
        does not exist must not become a flood of requests to CyberdyneAuth.

        The cooldown tracks *these* refetches only. An ordinary TTL-driven
        fetch must not arm it, or a key rotation occurring just after one would
        stay invisible until the cooldown lapsed.
        """
        moment = now or utcnow()
        if not may_refresh(self._last_refresh_attempt, moment, self._refresh_cooldown):
            logger.debug("jwks_refresh_suppressed_by_cooldown")
            return self._jwks.value if self._jwks else {}
        self._last_refresh_attempt = moment
        async with self._jwks_lock:
            return await self._fetch_jwks(self._jwks, moment)

    async def _fetch_jwks(
        self,
        cached: _Cached[dict[str, Any]] | None,
        now: datetime,
    ) -> dict[str, Any]:
        metadata = await self.metadata(now)
        try:
            response = await self._http.get(metadata.jwks_uri)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return self._fall_back(cached, now, self._jwks_window, "jwks", exc)
        keys: dict[str, Any] = response.json()
        self._jwks = _Cached(keys, now)
        return keys

    @staticmethod
    def _fall_back[T](
        cached: _Cached[T] | None,
        now: datetime,
        window: CacheWindow,
        document: str,
        exc: Exception,
    ) -> T:
        """Serve a stale document when the source is unreachable, or give up."""
        if cached is not None and window.is_usable_while_offline(cached.fetched_at, now):
            logger.warning(
                "auth_document_stale_but_usable",
                document=document,
                age_seconds=int((now - cached.fetched_at).total_seconds()),
            )
            return cached.value
        logger.error("auth_document_unavailable", document=document, error=type(exc).__name__)
        raise DependencyUnavailableError(
            f"CyberdyneAuth {document} is unavailable and no usable cached copy exists"
        ) from exc

    def key_for(self, kid: str | None, keys: dict[str, Any]) -> dict[str, Any] | None:
        """Find a JWK by `kid`, or the sole key when the token names none."""
        entries = keys.get("keys")
        if not isinstance(entries, list):
            raise InvalidTokenError("JWKS document has no keys")
        if kid is None:
            return entries[0] if len(entries) == 1 else None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("kid") == kid:
                return entry
        return None
