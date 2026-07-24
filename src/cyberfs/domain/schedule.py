"""Cron scheduling arithmetic.

A pure, dependency-free implementation of standard five-field cron, sufficient
for `BACKUP_CRON`. Kept in the domain so the "when does the next run fire"
decision is testable without a running loop -- the async loop that sleeps until
the returned time lives in infrastructure.

The five fields are `minute hour day-of-month month day-of-week`. Supported
syntax per field: `*`, single values, comma lists, `a-b` ranges, and `*/n` or
`a-b/n` steps. Day-of-week accepts `0` or `7` for Sunday. When both day-of-month
and day-of-week are restricted, a timestamp matches if *either* matches, which
is how Vixie cron behaves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# One year of minutes is a safe ceiling: every valid expression fires at least
# once within that window (Feb-29-only expressions still resolve inside four
# years, but BACKUP_CRON does not use them; the bound stays generous).
_SEARCH_LIMIT_MINUTES = 366 * 24 * 60

_FIELD_COUNT = 5
_MINUTE_RANGE = (0, 59)
_HOUR_RANGE = (0, 23)
_DOM_RANGE = (1, 31)
_MONTH_RANGE = (1, 12)
_DOW_RANGE = (0, 6)


class CronError(ValueError):
    """The cron expression is malformed or out of range."""


@dataclass(frozen=True, slots=True)
class _CronFields:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool


def _parse_part(part: str, low: int, high: int) -> set[int]:
    base, sep, step_text = part.partition("/")
    step = _parse_step(step_text) if sep else 1

    if base == "*":
        start, end = low, high
    elif "-" in base:
        start, end = _parse_range(base, low, high)
    else:
        start = end = _parse_value(base, low, high)

    return set(range(start, end + 1, step))


def _parse_step(text: str) -> int:
    if not text.isdigit() or int(text) <= 0:
        raise CronError(f"invalid step '{text}'")
    return int(text)


def _parse_value(text: str, low: int, high: int) -> int:
    if not (text.lstrip("-").isdigit()):
        raise CronError(f"invalid field value '{text}'")
    value = int(text)
    if not low <= value <= high:
        raise CronError(f"value {value} out of range {low}-{high}")
    return value


def _parse_range(text: str, low: int, high: int) -> tuple[int, int]:
    start_text, _, end_text = text.partition("-")
    start = _parse_value(start_text, low, high)
    end = _parse_value(end_text, low, high)
    if start > end:
        raise CronError(f"range start after end in '{text}'")
    return start, end


def _parse_field(spec: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"empty component in '{spec}'")
        values |= _parse_part(part, low, high)
    return frozenset(values)


def _parse_weekdays(spec: str) -> frozenset[int]:
    """Day-of-week, normalizing 7 to 0 (both mean Sunday)."""
    field = _parse_field(spec, _DOW_RANGE[0], 7)
    return frozenset(0 if day == 7 else day for day in field)


def _parse_cron(expr: str) -> _CronFields:
    parts = expr.split()
    if len(parts) != _FIELD_COUNT:
        raise CronError(f"expected {_FIELD_COUNT} fields, got {len(parts)}")
    minute, hour, dom, month, dow = parts
    return _CronFields(
        minutes=_parse_field(minute, *_MINUTE_RANGE),
        hours=_parse_field(hour, *_HOUR_RANGE),
        days=_parse_field(dom, *_DOM_RANGE),
        months=_parse_field(month, *_MONTH_RANGE),
        weekdays=_parse_weekdays(dow),
        dom_restricted=dom != "*",
        dow_restricted=dow != "*",
    )


def _cron_weekday(moment: datetime) -> int:
    """Convert Python's Monday=0 weekday to cron's Sunday=0."""
    return (moment.weekday() + 1) % 7


def _matches(moment: datetime, fields: _CronFields) -> bool:
    if moment.minute not in fields.minutes:
        return False
    if moment.hour not in fields.hours:
        return False
    if moment.month not in fields.months:
        return False
    return _day_matches(moment, fields)


def _day_matches(moment: datetime, fields: _CronFields) -> bool:
    dom_ok = moment.day in fields.days
    dow_ok = _cron_weekday(moment) in fields.weekdays
    if fields.dom_restricted and fields.dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_run_after(expr: str, after: datetime) -> datetime:
    """The first minute strictly after `after` that matches `expr`.

    Minute-granular: seconds and microseconds are dropped. Raises `CronError`
    for a malformed expression, or if no match falls within a year -- which a
    valid five-field expression never does.
    """
    fields = _parse_cron(expr)
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_SEARCH_LIMIT_MINUTES):
        if _matches(candidate, fields):
            return candidate
        candidate += timedelta(minutes=1)
    raise CronError(f"no run time for '{expr}' within a year")  # pragma: no cover
