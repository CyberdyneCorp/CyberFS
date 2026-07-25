"""Token acceptance and cache-freshness policy.

Pure decisions, separated from the HTTP that fetches the documents they act
on. The rule CyberdyneAuth's own docs are emphatic about -- derive issuer,
JWKS URI, and algorithm from discovery, never hard-code them -- is enforced
here by taking the discovered values as arguments: there is nowhere to bake a
constant in.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cyberfs.domain.errors import InvalidTokenError, TokenExpiredError

#: Never acceptable, whatever discovery says.
FORBIDDEN_ALGORITHMS = frozenset({"none", "NONE", "None"})


@dataclass(frozen=True, slots=True)
class DiscoveryMetadata:
    """The subset of the OIDC discovery document CyberFS validates against."""

    issuer: str
    jwks_uri: str
    signing_algorithms: tuple[str, ...]
    introspection_endpoint: str | None = None
    token_endpoint: str | None = None

    def __post_init__(self) -> None:
        if not self.issuer:
            raise InvalidTokenError("discovery document has no issuer")
        if not self.jwks_uri:
            raise InvalidTokenError("discovery document has no jwks_uri")
        usable = [alg for alg in self.signing_algorithms if alg not in FORBIDDEN_ALGORITHMS]
        if not usable:
            raise InvalidTokenError("discovery document advertises no usable signing algorithm")


def ensure_issuer(token_issuer: str | None, discovered_issuer: str) -> None:
    """The token's `iss` must equal the discovered issuer string exactly."""
    if token_issuer != discovered_issuer:
        raise InvalidTokenError("token issuer does not match the discovered issuer")


def ensure_algorithm(algorithm: str | None, discovered: Collection[str]) -> None:
    """Reject `none` and anything discovery does not advertise."""
    if not algorithm or algorithm in FORBIDDEN_ALGORITHMS:
        raise InvalidTokenError("token algorithm is not permitted")
    if algorithm not in discovered:
        raise InvalidTokenError("token algorithm is not advertised by discovery")


def ensure_not_expired(
    expires_at: datetime | None,
    now: datetime,
    skew: timedelta,
) -> None:
    if expires_at is None:
        raise InvalidTokenError("token has no expiry")
    if expires_at + skew <= now:
        raise TokenExpiredError()


def ensure_not_before(
    not_before: datetime | None,
    now: datetime,
    skew: timedelta,
) -> None:
    """`nbf`/`iat` are checked only when present."""
    if not_before is not None and not_before - skew > now:
        raise InvalidTokenError("token is not yet valid")


# --- cached document freshness --------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheWindow:
    """How long a fetched document may be reused.

    `ttl` is the normal reuse window. `stale_max` is how far past it the
    document may still be used *when the source is unreachable* -- an auth
    outage should not take CyberFS down while a usable key set is in hand.
    """

    ttl: timedelta
    stale_max: timedelta

    def is_fresh(self, fetched_at: datetime, now: datetime) -> bool:
        return now - fetched_at < self.ttl

    def is_usable_while_offline(self, fetched_at: datetime, now: datetime) -> bool:
        return now - fetched_at <= self.stale_max


def may_refresh(
    last_attempt_at: datetime | None,
    now: datetime,
    cooldown: timedelta,
) -> bool:
    """Whether an unknown `kid` may trigger another JWKS fetch.

    Without a cooldown, a stream of tokens signed by a key that genuinely does
    not exist would hammer CyberdyneAuth once per request.
    """
    if last_attempt_at is None:
        return True
    return now - last_attempt_at >= cooldown


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
