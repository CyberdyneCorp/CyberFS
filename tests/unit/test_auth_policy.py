"""Token acceptance and cache-freshness policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.domain.auth.policy import (
    CacheWindow,
    DiscoveryMetadata,
    ensure_algorithm,
    ensure_issuer,
    ensure_not_before,
    ensure_not_expired,
    may_refresh,
    utcnow,
)
from cyberfs.domain.errors import InvalidTokenError, TokenExpiredError

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
SKEW = timedelta(seconds=60)
ISSUER = "https://auth.backend.coolify.cyberdynecorp.ai"


def metadata(**overrides: object) -> DiscoveryMetadata:
    base: dict[str, object] = {
        "issuer": ISSUER,
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "signing_algorithms": ("RS256",),
    }
    return DiscoveryMetadata(**{**base, **overrides})  # type: ignore[arg-type]


# --- discovery metadata ----------------------------------------------------


def test_metadata_requires_an_issuer() -> None:
    with pytest.raises(InvalidTokenError, match="issuer"):
        metadata(issuer="")


def test_metadata_requires_a_jwks_uri() -> None:
    with pytest.raises(InvalidTokenError, match="jwks_uri"):
        metadata(jwks_uri="")


def test_metadata_requires_a_usable_algorithm() -> None:
    with pytest.raises(InvalidTokenError, match="usable signing algorithm"):
        metadata(signing_algorithms=("none",))


# --- issuer ----------------------------------------------------------------


def test_matching_issuer_is_accepted() -> None:
    ensure_issuer(ISSUER, ISSUER)


@pytest.mark.parametrize(
    "token_issuer",
    [
        None,
        "",
        "cyberdyne-auth",  # the hard-coded value that caused their #47/#114
        f"{ISSUER}/",  # trailing slash is a different string
        "https://evil.example.test",
    ],
)
def test_mismatched_issuer_is_rejected(token_issuer: str | None) -> None:
    with pytest.raises(InvalidTokenError, match="issuer"):
        ensure_issuer(token_issuer, ISSUER)


# --- algorithm -------------------------------------------------------------


def test_advertised_algorithm_is_accepted() -> None:
    ensure_algorithm("RS256", ("RS256", "HS256"))


@pytest.mark.parametrize("algorithm", ["none", "NONE", "None"])
def test_unsigned_token_is_rejected_even_if_advertised(algorithm: str) -> None:
    with pytest.raises(InvalidTokenError, match="not permitted"):
        ensure_algorithm(algorithm, ("RS256", algorithm))


@pytest.mark.parametrize("algorithm", [None, ""])
def test_absent_algorithm_is_rejected(algorithm: str | None) -> None:
    with pytest.raises(InvalidTokenError, match="not permitted"):
        ensure_algorithm(algorithm, ("RS256",))


def test_undiscovered_algorithm_is_rejected() -> None:
    with pytest.raises(InvalidTokenError, match="not advertised"):
        ensure_algorithm("HS256", ("RS256",))


# --- expiry ----------------------------------------------------------------


def test_future_expiry_is_accepted() -> None:
    ensure_not_expired(NOW + timedelta(minutes=5), NOW, SKEW)


def test_token_without_expiry_is_rejected() -> None:
    with pytest.raises(InvalidTokenError, match="no expiry"):
        ensure_not_expired(None, NOW, SKEW)


def test_expired_token_is_rejected() -> None:
    with pytest.raises(TokenExpiredError):
        ensure_not_expired(NOW - timedelta(minutes=5), NOW, SKEW)


def test_expiry_within_skew_is_tolerated() -> None:
    """Up to 60 seconds of clock skew, per the spec."""
    ensure_not_expired(NOW - timedelta(seconds=30), NOW, SKEW)


def test_expiry_beyond_skew_is_rejected() -> None:
    with pytest.raises(TokenExpiredError):
        ensure_not_expired(NOW - timedelta(seconds=61), NOW, SKEW)


def test_not_before_is_checked_only_when_present() -> None:
    ensure_not_before(None, NOW, SKEW)
    ensure_not_before(NOW - timedelta(minutes=1), NOW, SKEW)


def test_token_not_yet_valid_is_rejected() -> None:
    with pytest.raises(InvalidTokenError, match="not yet valid"):
        ensure_not_before(NOW + timedelta(minutes=5), NOW, SKEW)


def test_not_before_within_skew_is_tolerated() -> None:
    ensure_not_before(NOW + timedelta(seconds=30), NOW, SKEW)


# --- cache windows ---------------------------------------------------------


WINDOW = CacheWindow(ttl=timedelta(hours=1), stale_max=timedelta(hours=24))


def test_document_within_ttl_is_fresh() -> None:
    assert WINDOW.is_fresh(NOW - timedelta(minutes=59), NOW)


def test_document_past_ttl_is_not_fresh() -> None:
    assert not WINDOW.is_fresh(NOW - timedelta(hours=2), NOW)


def test_stale_document_is_usable_while_the_source_is_offline() -> None:
    """An auth blip must not take CyberFS down while a key set is in hand."""
    assert WINDOW.is_usable_while_offline(NOW - timedelta(hours=12), NOW)


def test_document_past_the_stale_window_is_unusable() -> None:
    assert not WINDOW.is_usable_while_offline(NOW - timedelta(hours=25), NOW)


# --- refresh cooldown ------------------------------------------------------

COOLDOWN = timedelta(seconds=60)


def test_first_refresh_is_always_permitted() -> None:
    assert may_refresh(None, NOW, COOLDOWN)


def test_refresh_within_cooldown_is_suppressed() -> None:
    """A flood of tokens naming a nonexistent kid must not become a flood of fetches."""
    assert not may_refresh(NOW - timedelta(seconds=30), NOW, COOLDOWN)


def test_refresh_after_cooldown_is_permitted() -> None:
    assert may_refresh(NOW - timedelta(seconds=61), NOW, COOLDOWN)


def test_refresh_exactly_at_cooldown_is_permitted() -> None:
    assert may_refresh(NOW - COOLDOWN, NOW, COOLDOWN)


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo is not None
