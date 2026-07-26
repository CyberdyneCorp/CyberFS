"""Async cron scheduler: firing, overlap prevention, clean stop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from cyberfs.infrastructure import scheduler as scheduler_module
from cyberfs.infrastructure.scheduler import CronScheduler


def _clock(moment: datetime = datetime(2026, 7, 24, 2, 59, tzinfo=UTC)) -> object:
    return lambda: moment


async def test_trigger_runs_the_callback() -> None:
    calls = 0

    async def callback() -> None:
        nonlocal calls
        calls += 1

    sched = CronScheduler("0 3 * * *", callback, clock=_clock())
    assert await sched.trigger() is True
    assert calls == 1
    assert not sched.is_running


async def test_overlapping_trigger_is_skipped() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0
    skips = 0

    async def callback() -> None:
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()

    async def on_skip() -> None:
        nonlocal skips
        skips += 1

    sched = CronScheduler("* * * * *", callback, clock=_clock(), on_skip=on_skip)

    first = asyncio.create_task(sched.trigger())
    await started.wait()
    assert sched.is_running

    # A second fire while the first is still in flight must be skipped.
    assert await sched.trigger() is False
    assert skips == 1

    release.set()
    assert await first is True
    assert runs == 1


async def test_seconds_until_next_is_nonnegative() -> None:
    sched = CronScheduler("0 3 * * *", _noop, clock=_clock())
    # 2:59 to 3:00 is 60 seconds.
    assert sched.seconds_until_next() == 60.0


async def test_loop_fires_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    fired = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds: float) -> None:
        # Collapse the schedule wait but still yield to the loop.
        await real_sleep(0)

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fast_sleep)

    async def callback() -> None:
        fired.set()

    sched = CronScheduler("* * * * *", callback, clock=_clock())
    await sched.start()
    # Idempotent start does not spawn a second task.
    await sched.start()
    await asyncio.wait_for(fired.wait(), timeout=1.0)
    await sched.stop()


async def test_loop_survives_an_unreadable_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a schedule that fails to parse used to kill the loop.

    `seconds_until_next` ran outside the guard, so the exception escaped
    `_loop`, the task died, and the service stayed healthy while the job never
    ran again. Settings now rejects such a value at boot; this is the backstop
    for anything that still reaches the loop.
    """
    real_sleep = asyncio.sleep
    fired = asyncio.Event()
    calls = 0

    async def fast_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fast_sleep)

    async def callback() -> None:
        fired.set()

    sched = CronScheduler("* * * * *", callback, clock=_clock())

    def flaky_next() -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("schedule cannot be parsed")
        return 0.0

    monkeypatch.setattr(sched, "seconds_until_next", flaky_next)

    await sched.start()
    # The loop must reach a second computation and then fire, rather than die.
    await asyncio.wait_for(fired.wait(), timeout=1.0)
    await sched.stop()
    assert calls >= 2


async def test_an_unreadable_schedule_backs_off_rather_than_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def callback() -> None:  # pragma: no cover - never reached
        raise AssertionError("callback must not run")

    sched = CronScheduler("* * * * *", callback, clock=_clock())
    monkeypatch.setattr(
        sched, "seconds_until_next", lambda: (_ for _ in ()).throw(ValueError("bad"))
    )

    # A zero delay here would spin the event loop hot on a broken schedule.
    assert sched._next_delay() == scheduler_module._UNREADABLE_SCHEDULE_BACKOFF_SECONDS


async def test_loop_survives_a_failing_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep
    attempts = 0
    recovered = asyncio.Event()

    async def fast_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fast_sleep)

    async def callback() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first run explodes")
        recovered.set()

    sched = CronScheduler("* * * * *", callback, clock=_clock())
    await sched.start()
    await asyncio.wait_for(recovered.wait(), timeout=1.0)
    await sched.stop()
    assert attempts >= 2


async def test_stop_without_start_is_a_noop() -> None:
    sched = CronScheduler("0 3 * * *", _noop, clock=_clock())
    await sched.stop()  # must not raise


async def _noop() -> None:
    return None
