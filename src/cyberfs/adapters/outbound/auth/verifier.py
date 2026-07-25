"""Bearer token verification against CyberdyneAuth.

Signature checking is delegated to PyJWT; everything CyberFS decides for
itself -- issuer, algorithm, expiry -- goes through `domain.auth.policy`, which
takes the discovered values as arguments so no constant can be baked in.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import jwt
from jwt import PyJWK

from cyberfs.adapters.outbound.auth.discovery import DiscoveryClient
from cyberfs.domain.auth.claims import principal_from_claims
from cyberfs.domain.auth.policy import (
    ensure_algorithm,
    ensure_issuer,
    ensure_not_expired,
    utcnow,
)
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import InvalidTokenError, TokenExpiredError


class JwtTokenVerifier:
    """Implements the `TokenVerifier` port."""

    def __init__(self, discovery: DiscoveryClient, *, clock_skew: timedelta) -> None:
        self._discovery = discovery
        self._skew = clock_skew

    async def verify(self, token: str) -> Principal:
        now = utcnow()
        metadata = await self._discovery.metadata(now)

        header = self._header(token)
        # Checked before touching the JWKS: an `alg: none` token must be
        # rejected outright, never used to select a key.
        ensure_algorithm(header.get("alg"), metadata.signing_algorithms)

        key = await self._resolve_key(header.get("kid"), now)
        claims = self._decode(token, key, metadata.signing_algorithms)

        ensure_issuer(claims.get("iss"), metadata.issuer)
        principal = principal_from_claims(claims)
        ensure_not_expired(principal.expires_at, now, self._skew)
        return principal

    @staticmethod
    def _header(token: str) -> dict[str, Any]:
        try:
            return jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("token header is malformed") from exc

    async def _resolve_key(self, kid: str | None, now: Any) -> PyJWK:
        keys = await self._discovery.jwks(now)
        entry = self._discovery.key_for(kid, keys)
        if entry is None:
            # An unknown kid is the normal signal that keys rotated.
            keys = await self._discovery.refresh_jwks_for_unknown_kid(now)
            entry = self._discovery.key_for(kid, keys)
        if entry is None:
            raise InvalidTokenError("token was signed by an unknown key")
        try:
            return PyJWK.from_dict(entry)
        except Exception as exc:
            raise InvalidTokenError("signing key is unusable") from exc

    def _decode(self, token: str, key: PyJWK, algorithms: tuple[str, ...]) -> dict[str, Any]:
        try:
            decoded: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=list(algorithms),
                options={
                    # `aud` is checked by us, not PyJWT: CyberdyneAuth user
                    # tokens carry no audience today, and requiring one would
                    # reject every real token.
                    "verify_aud": False,
                    # Issuer and expiry go through domain policy so the rules
                    # live in one testable place.
                    "verify_iss": False,
                    "verify_exp": False,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError() from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("token signature is not valid") from exc
        return decoded
