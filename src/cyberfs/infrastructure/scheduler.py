"""Cron-driven background scheduling.

An async loop that computes the next fire time with the pure `next_run_after`
arithmetic, sleeps until then, and invokes a callback -- with overlap
prevention, so a run that fires while the previous one is still going is skipped
and recorded rather than started concurrently (`backup-restore/spec.md`,
"Overlapping runs prevented").

The scheduler is deliberately generic: it drives the backup job here, and can
drive the purge/reaper/reconcile sweeps the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime

from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.schedule import next_run_after
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

Callback = Callable[[], Awaitable[None]]
SkipHook = Callable[[], Awaitable[None]]
Clock = Callable[[], datetime]


class CronScheduler:
    """Runs `callback` on the `cron` schedule, never two at once.

    `start()`/`stop()` bracket the background task for the app lifespan. A manual
    trigger reuses `trigger()`, so an admin-initiated run gets the same overlap
    guard as a scheduled one.
    """

    def __init__(
        self,
        cron: str,
        callback: Callback,
        *,
        name: str = "job",
        clock: Clock = utcnow,
        on_skip: SkipHook | None = None,
    ) -> None:
        self._cron = cron
        self._callback = callback
        self._name = name
        self._clock = clock
        self._on_skip = on_skip
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether a run is in flight right now."""
        return self._running

    async def trigger(self) -> bool:
        """Run the callback unless one is already in flight.

        Returns True if the callback ran, False if it was skipped. The
        check-and-set is synchronous (no await between), so two triggers racing
        on the single event loop cannot both start.
        """
        if self._running:
            logger.warning("scheduled_run_skipped", job=self._name)
            if self._on_skip is not None:
                await self._on_skip()
            return False
        self._running = True
        try:
            await self._callback()
        finally:
            self._running = False
        return True

    def seconds_until_next(self) -> float:
        """Seconds from now until the next scheduled fire, never negative."""
        now = self._clock()
        nxt = next_run_after(self._cron, now)
        return max(0.0, (nxt - now).total_seconds())

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name=f"cron-{self._name}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.seconds_until_next())
            try:
                await self.trigger()
            except asyncio.CancelledError:
                raise
            except Exception:  # a job error must not kill the loop
                logger.exception("scheduled_run_failed", job=self._name)
