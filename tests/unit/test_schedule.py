"""Pure cron arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cyberfs.domain.schedule import CronError, next_run_after


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- basic matching --------------------------------------------------------


def test_daily_at_three_am() -> None:
    # The default BACKUP_CRON.
    after = _dt(2026, 7, 24, 12, 0)
    assert next_run_after("0 3 * * *", after) == _dt(2026, 7, 25, 3, 0)


def test_every_minute_advances_by_one() -> None:
    after = _dt(2026, 7, 24, 12, 0)
    assert next_run_after("* * * * *", after) == _dt(2026, 7, 24, 12, 1)


def test_result_is_strictly_after_input() -> None:
    # Exactly on a matching minute: the next match is the following one.
    at_three = _dt(2026, 7, 24, 3, 0)
    assert next_run_after("0 3 * * *", at_three) == _dt(2026, 7, 25, 3, 0)


def test_seconds_are_dropped() -> None:
    after = _dt(2026, 7, 24, 2, 59).replace(second=30, microsecond=500)
    assert next_run_after("0 3 * * *", after) == _dt(2026, 7, 24, 3, 0)


def test_preserves_timezone() -> None:
    result = next_run_after("0 3 * * *", _dt(2026, 7, 24, 12, 0))
    assert result.tzinfo is UTC


# --- lists, ranges, steps --------------------------------------------------


def test_minute_list() -> None:
    after = _dt(2026, 7, 24, 12, 10)
    assert next_run_after("0,15,30,45 * * * *", after) == _dt(2026, 7, 24, 12, 15)


def test_hour_range() -> None:
    after = _dt(2026, 7, 24, 7, 30)
    assert next_run_after("0 9-17 * * *", after) == _dt(2026, 7, 24, 9, 0)


def test_step_every_fifteen_minutes() -> None:
    after = _dt(2026, 7, 24, 12, 1)
    assert next_run_after("*/15 * * * *", after) == _dt(2026, 7, 24, 12, 15)


def test_range_with_step() -> None:
    after = _dt(2026, 7, 24, 12, 0)
    # Minutes 10,12,14 within 10-15.
    assert next_run_after("10-15/2 * * * *", after) == _dt(2026, 7, 24, 12, 10)


def test_month_rollover() -> None:
    after = _dt(2026, 7, 31, 23, 30)
    assert next_run_after("0 0 1 * *", after) == _dt(2026, 8, 1, 0, 0)


def test_year_rollover() -> None:
    after = _dt(2026, 12, 31, 23, 30)
    assert next_run_after("0 0 1 1 *", after) == _dt(2027, 1, 1, 0, 0)


# --- day-of-week -----------------------------------------------------------


def test_weekday_sunday_zero() -> None:
    # 2026-07-24 is a Friday; next Sunday is the 26th.
    after = _dt(2026, 7, 24, 12, 0)
    assert next_run_after("0 0 * * 0", after) == _dt(2026, 7, 26, 0, 0)


def test_weekday_sunday_seven_equals_zero() -> None:
    after = _dt(2026, 7, 24, 12, 0)
    assert next_run_after("0 0 * * 7", after) == _dt(2026, 7, 26, 0, 0)


def test_weekday_monday() -> None:
    # Next Monday after Friday the 24th is the 27th.
    after = _dt(2026, 7, 24, 12, 0)
    assert next_run_after("0 0 * * 1", after) == _dt(2026, 7, 27, 0, 0)


def test_dom_and_dow_both_restricted_is_or() -> None:
    # Vixie cron: match day 1 OR Sunday. The 26th (Sunday) comes before the
    # next 1st of the month.
    after = _dt(2026, 7, 24, 12, 0)
    assert next_run_after("0 0 1 * 0", after) == _dt(2026, 7, 26, 0, 0)


def test_dom_only_restricted_is_and_wildcard() -> None:
    after = _dt(2026, 7, 24, 12, 0)
    assert next_run_after("0 0 15 * *", after) == _dt(2026, 8, 15, 0, 0)


def test_restricted_month_skips_intervening_midnights() -> None:
    # From July, a December-only run passes the minute/hour match on every
    # month's first midnight but is rejected until the month matches.
    after = _dt(2026, 7, 1, 12, 0)
    assert next_run_after("0 0 1 12 *", after) == _dt(2026, 12, 1, 0, 0)


# --- errors ----------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "0 0 32 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "*/0 * * * *",
        "abc * * * *",
        "5-1 * * * *",
        "1,,2 * * * *",
        "1/x * * * *",
    ],
)
def test_malformed_expressions_raise(expr: str) -> None:
    with pytest.raises(CronError):
        next_run_after(expr, _dt(2026, 7, 24, 12, 0))
