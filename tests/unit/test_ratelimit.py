"""Fixed-window rate limiting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cyberfs.domain.ratelimit import FixedWindowLimiter

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def limiter(limit: int = 3) -> FixedWindowLimiter:
    return FixedWindowLimiter(limit=limit, window=MINUTE)


def test_unknown_key_is_not_limited() -> None:
    assert not limiter().is_limited("1.2.3.4", NOW)


def test_below_the_limit_is_allowed() -> None:
    subject = limiter(limit=3)
    for _ in range(2):
        subject.record("1.2.3.4", NOW)
    assert not subject.is_limited("1.2.3.4", NOW)


def test_at_the_limit_is_blocked() -> None:
    subject = limiter(limit=3)
    for _ in range(3):
        subject.record("1.2.3.4", NOW)
    assert subject.is_limited("1.2.3.4", NOW)


def test_keys_are_independent() -> None:
    subject = limiter(limit=1)
    subject.record("1.2.3.4", NOW)
    assert subject.is_limited("1.2.3.4", NOW)
    assert not subject.is_limited("5.6.7.8", NOW)


def test_window_rolls_over() -> None:
    subject = limiter(limit=1)
    subject.record("1.2.3.4", NOW)
    assert not subject.is_limited("1.2.3.4", NOW + MINUTE)


def test_recording_after_rollover_starts_a_new_window() -> None:
    subject = limiter(limit=2)
    subject.record("1.2.3.4", NOW)
    subject.record("1.2.3.4", NOW)
    assert subject.is_limited("1.2.3.4", NOW)
    subject.record("1.2.3.4", NOW + MINUTE)
    assert not subject.is_limited("1.2.3.4", NOW + MINUTE)


def test_zero_limit_disables_the_limiter() -> None:
    """`0` disables a limit, matching how CyberdyneAuth treats its RATELIMIT_* settings."""
    subject = limiter(limit=0)
    for _ in range(100):
        subject.record("1.2.3.4", NOW)
    assert not subject.is_limited("1.2.3.4", NOW)
    assert not subject.enabled


def test_retry_after_counts_down_within_the_window() -> None:
    subject = limiter(limit=1)
    subject.record("1.2.3.4", NOW)
    assert subject.retry_after("1.2.3.4", NOW + timedelta(seconds=20)) == timedelta(seconds=40)


def test_retry_after_is_zero_when_not_limited() -> None:
    subject = limiter(limit=5)
    subject.record("1.2.3.4", NOW)
    assert subject.retry_after("1.2.3.4", NOW) == timedelta(0)


def test_retry_after_is_zero_for_an_unknown_key() -> None:
    assert limiter().retry_after("nobody", NOW) == timedelta(0)


def test_reset_clears_a_key() -> None:
    subject = limiter(limit=1)
    subject.record("1.2.3.4", NOW)
    subject.reset("1.2.3.4")
    assert not subject.is_limited("1.2.3.4", NOW)


def test_evict_expired_drops_rolled_over_windows() -> None:
    subject = limiter(limit=1)
    subject.record("old", NOW)
    subject.record("new", NOW + MINUTE)
    assert subject.evict_expired(NOW + MINUTE) == 1
    assert subject.is_limited("new", NOW + MINUTE)


def test_evict_expired_keeps_live_windows() -> None:
    subject = limiter(limit=1)
    subject.record("live", NOW)
    assert subject.evict_expired(NOW + timedelta(seconds=30)) == 0
