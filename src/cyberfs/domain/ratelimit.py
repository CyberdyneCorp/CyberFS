"""Fixed-window rate limiting.

Used for auth failures per source IP and for public-link passphrase attempts.
The algorithm is pure and clock-injected so it can be tested without sleeping;
the in-memory store here is per-replica. A shared Redis-backed store lands
with the cache adapter, at which point the same port is implemented against
Redis and the limit becomes deployment-wide rather than per-replica.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class _Window:
    started_at: datetime
    count: int


@dataclass
class FixedWindowLimiter:
    """Counts events per key within a fixed window.

    A limit of 0 disables the limiter entirely, matching how CyberdyneAuth
    treats its own `RATELIMIT_*` settings.
    """

    limit: int
    window: timedelta
    _windows: dict[str, _Window] = field(default_factory=dict, repr=False)

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def record(self, key: str, now: datetime) -> None:
        """Count one event against `key`."""
        if not self.enabled:
            return
        window = self._windows.get(key)
        if window is None or now - window.started_at >= self.window:
            self._windows[key] = _Window(started_at=now, count=1)
            return
        self._windows[key] = _Window(started_at=window.started_at, count=window.count + 1)

    def is_limited(self, key: str, now: datetime) -> bool:
        """Whether `key` has exhausted its allowance for the current window."""
        if not self.enabled:
            return False
        window = self._windows.get(key)
        if window is None or now - window.started_at >= self.window:
            return False
        return window.count >= self.limit

    def retry_after(self, key: str, now: datetime) -> timedelta:
        """How long until `key`'s window resets. Zero when not limited."""
        window = self._windows.get(key)
        if window is None or not self.is_limited(key, now):
            return timedelta(0)
        remaining = window.started_at + self.window - now
        return max(remaining, timedelta(0))

    def reset(self, key: str) -> None:
        self._windows.pop(key, None)

    def evict_expired(self, now: datetime) -> int:
        """Drop windows that have rolled over, so the map does not grow forever."""
        stale = [key for key, w in self._windows.items() if now - w.started_at >= self.window]
        for key in stale:
            del self._windows[key]
        return len(stale)
