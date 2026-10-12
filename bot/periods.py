"""Pure UTC period arithmetic.

All periods are half-open ranges ``[start, end)`` in UTC:

* daily   — key ``YYYY-MM-DD``, 00:00 to the next day's 00:00
* weekly  — key ``YYYY-Www`` (ISO week), Monday 00:00 to the next Monday 00:00
* monthly — key ``YYYY-MM``, the 1st 00:00 to the next month's 1st 00:00

Every period boundary falls on a UTC midnight, so the ``daily_counts`` buckets
partition every period exactly. Nothing in this module reads the clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from bot.clock import UTC, ensure_utc

_ONE_DAY = timedelta(days=1)
_EPSILON = timedelta(microseconds=1)


class PeriodType(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


RESET_TYPES: tuple[PeriodType, ...] = (PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY)

# Board types: the three resettable periods plus the never-resetting all-time board.
BOARD_ORDER: tuple[str, ...] = ("daily", "weekly", "monthly", "alltime")
ALLTIME = "alltime"

_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True)
class Period:
    type: PeriodType
    key: str
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        moment = ensure_utc(moment)
        return self.start <= moment < self.end


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def utc_day_key(moment: datetime) -> str:
    """Canonical UTC calendar date (``YYYY-MM-DD``) for a moment."""
    return ensure_utc(moment).date().isoformat()


def _daily(d: date) -> Period:
    start = _midnight(d)
    return Period(PeriodType.DAILY, d.isoformat(), start, start + _ONE_DAY)


def _weekly(d: date) -> Period:
    monday = d - timedelta(days=d.weekday())
    iso_year, iso_week, _ = monday.isocalendar()
    start = _midnight(monday)
    return Period(PeriodType.WEEKLY, f"{iso_year:04d}-W{iso_week:02d}", start, start + 7 * _ONE_DAY)


def _monthly(year: int, month: int) -> Period:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC) if month == 12 else datetime(year, month + 1, 1, tzinfo=UTC)
    return Period(PeriodType.MONTHLY, f"{year:04d}-{month:02d}", start, end)


def period_containing(ptype: PeriodType | str, moment: datetime) -> Period:
    """Return the period of ``ptype`` that contains ``moment``."""
    ptype = PeriodType(ptype)
    d = ensure_utc(moment).date()
    if ptype is PeriodType.DAILY:
        return _daily(d)
    if ptype is PeriodType.WEEKLY:
        return _weekly(d)
    return _monthly(d.year, d.month)


def period_after(period: Period) -> Period:
    return period_containing(period.type, period.end)


def period_before(period: Period) -> Period:
    return period_containing(period.type, period.start - _EPSILON)


def parse_period_key(ptype: PeriodType | str, key: str) -> Period:
    """Rebuild a period from its canonical key. Raises ``ValueError`` if invalid."""
    ptype = PeriodType(ptype)
    if ptype is PeriodType.DAILY:
        m = _DAY_RE.match(key)
        if not m:
            raise ValueError(f"invalid daily key {key!r}")
        return _daily(date(int(m[1]), int(m[2]), int(m[3])))
    if ptype is PeriodType.WEEKLY:
        m = _WEEK_RE.match(key)
        if not m:
            raise ValueError(f"invalid weekly key {key!r}")
        return _weekly(date.fromisocalendar(int(m[1]), int(m[2]), 1))
    m = _MONTH_RE.match(key)
    if not m:
        raise ValueError(f"invalid monthly key {key!r}")
    month = int(m[2])
    if not 1 <= month <= 12:
        raise ValueError(f"invalid monthly key {key!r}")
    return _monthly(int(m[1]), month)


def ended_periods(
    ptype: PeriodType | str,
    installed_at: datetime,
    now: datetime,
    *,
    after: datetime | None = None,
) -> list[Period]:
    """Periods whose boundary has passed and that did not end before installation.

    A period is included when ``installed_at < period.end <= now``. The first
    partial period after installation is therefore included once it ends, and
    periods that ended at or before installation never are. When ``after`` is
    given (the end of the last period already recorded) enumeration starts at
    the period beginning there, so already-recorded periods are not revisited.
    """
    ptype = PeriodType(ptype)
    installed_at = ensure_utc(installed_at)
    now = ensure_utc(now)
    period = period_containing(ptype, ensure_utc(after) if after is not None else installed_at)
    result: list[Period] = []
    while period.end <= now:
        if period.end > installed_at:
            result.append(period)
        period = period_after(period)
    return result


def next_reset(ptype: PeriodType | str, now: datetime) -> datetime:
    """The upcoming boundary at which the current ``ptype`` period ends."""
    return period_containing(ptype, now).end


def day_range(start: datetime, end: datetime) -> tuple[str, str]:
    """Inclusive start / exclusive end ``daily_counts.day`` keys covering ``[start, end)``.

    For the aligned daily/weekly/monthly periods this is exact. For a range that
    ends mid-day (forced test resets end at "now") the final day is included in
    full; that is only exact while the range is computed promptly, which is why
    the reset machine refuses to compute a forced test run late.
    """
    start = ensure_utc(start)
    end = ensure_utc(end)
    if end <= start:
        key = utc_day_key(start)
        return key, key
    last_day = (end - _EPSILON).date()
    return utc_day_key(start), (last_day + _ONE_DAY).isoformat()


def board_range(board_type: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """The live range shown by a board: the current period, or everything for all-time."""
    if board_type == ALLTIME:
        return None, None
    period = period_containing(board_type, now)
    return period.start, period.end


def unix_timestamp(moment: datetime) -> int:
    return int(ensure_utc(moment).timestamp())
