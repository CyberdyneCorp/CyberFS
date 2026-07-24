"""A conformant fake CyberdyneAuth, for exercising the auth adapters.

Real RSA keys and real signatures -- the point is to test our verification,
not to test that a stub returns what we told it to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://auth.example.test"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
INTROSPECTION_URL = f"{ISSUER}/oauth/introspect"
TOKEN_URL = f"{ISSUER}/oauth/token"


class SigningKey:
    """An RSA key plus its JWK representation."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwk(self) -> dict[str, Any]:
        exported: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(self._private.public_key()))
        exported["kid"] = self.kid
        exported["alg"] = "RS256"
        exported["use"] = "sig"
        return exported

    def sign(self, claims: dict[str, Any], *, algorithm: str = "RS256") -> str:
        return jwt.encode(claims, self._private, algorithm=algorithm, headers={"kid": self.kid})


def discovery_document(
    *,
    issuer: str = ISSUER,
    jwks_uri: str = JWKS_URL,
    algorithms: tuple[str, ...] = ("RS256",),
) -> dict[str, Any]:
    return {
        "issuer": issuer,
        "jwks_uri": jwks_uri,
        "id_token_signing_alg_values_supported": list(algorithms),
        "introspection_endpoint": INTROSPECTION_URL,
        "token_endpoint": TOKEN_URL,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
    }


def jwks_document(*keys: SigningKey) -> dict[str, Any]:
    return {"keys": [key.jwk for key in keys]}


def user_claims(
    subject: str = "user-1",
    *,
    issuer: str = ISSUER,
    is_admin: bool = False,
    expires_in: timedelta = timedelta(minutes=15),
    **extra: Any,
) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    return {
        "sub": subject,
        "iss": issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "is_admin": is_admin,
        **extra,
    }
