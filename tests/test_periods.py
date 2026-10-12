"""Pure period arithmetic: boundaries, ISO weeks, months, enumeration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.clock import UTC
from bot.periods import (
    PeriodType,
    board_range,
    day_range,
    ended_periods,
    next_reset,
    parse_period_key,
    period_after,
    period_before,
    period_containing,
    unix_timestamp,
    utc_day_key,
)


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_daily_boundaries_and_half_open_range() -> None:
    p = period_containing("daily", dt(2026, 3, 10, 23, 59, 59, 999999))
    assert p.key == "2026-03-10"
    assert p.start == dt(2026, 3, 10)
    assert p.end == dt(2026, 3, 11)
    assert p.contains(p.start)
    assert not p.contains(p.end)
    assert period_containing("daily", dt(2026, 3, 11)).key == "2026-03-11"


def test_message_at_235959_belongs_to_that_day() -> None:
    assert utc_day_key(dt(2026, 3, 10, 23, 59, 59)) == "2026-03-10"
    assert period_containing("daily", dt(2026, 3, 10, 23, 59, 59)).key == "2026-03-10"


def test_utc_conversion_of_non_utc_times() -> None:
    eastern = timezone(timedelta(hours=-5))
    local = datetime(2026, 3, 10, 22, 0, tzinfo=eastern)  # 03:00 UTC on the 11th
    assert utc_day_key(local) == "2026-03-11"
    assert period_containing("daily", local).key == "2026-03-11"
    # Naive datetimes are treated as UTC.
    assert utc_day_key(datetime(2026, 3, 10, 23, 0)) == "2026-03-10"


def test_iso_week_basic() -> None:
    p = period_containing("weekly", dt(2026, 3, 10, 12))  # Tuesday
    assert p.key == "2026-W11"
    assert p.start == dt(2026, 3, 9)  # Monday
    assert p.end == dt(2026, 3, 16)
    assert period_containing("weekly", dt(2026, 3, 15, 23, 59, 59)).key == "2026-W11"
    assert period_containing("weekly", dt(2026, 3, 16)).key == "2026-W12"


def test_iso_week_53_and_year_transition() -> None:
    # 2026 has 53 ISO weeks (Dec 31 2026 is a Thursday).
    p = period_containing("weekly", dt(2026, 12, 31))
    assert p.key == "2026-W53"
    assert p.start == dt(2026, 12, 28)
    assert p.end == dt(2027, 1, 4)
    # Jan 1-3 2027 still belong to 2026-W53.
    assert period_containing("weekly", dt(2027, 1, 3, 23)).key == "2026-W53"
    assert period_containing("weekly", dt(2027, 1, 4)).key == "2027-W01"
    # 2020 also had week 53.
    assert period_containing("weekly", dt(2021, 1, 1)).key == "2020-W53"
    # ISO year can be ahead of the calendar year.
    assert period_containing("weekly", dt(2024, 12, 30)).key == "2025-W01"


def test_month_boundaries_jan31_to_feb1_and_december() -> None:
    jan = period_containing("monthly", dt(2026, 1, 31, 23, 59, 59))
    assert jan.key == "2026-01"
    assert jan.end == dt(2026, 2, 1)
    feb = period_containing("monthly", dt(2026, 2, 1))
    assert feb.key == "2026-02"
    assert feb.end - feb.start == timedelta(days=28)
    dec = period_containing("monthly", dt(2026, 12, 15))
    assert dec.end == dt(2027, 1, 1)
    assert period_after(dec).key == "2027-01"


def test_leap_day() -> None:
    assert period_containing("daily", dt(2024, 2, 29, 12)).key == "2024-02-29"
    feb = period_containing("monthly", dt(2024, 2, 29))
    assert feb.end == dt(2024, 3, 1)
    assert feb.end - feb.start == timedelta(days=29)
    assert period_after(period_containing("daily", dt(2024, 2, 28))).key == "2024-02-29"


@pytest.mark.parametrize(
    ("ptype", "key"),
    [("daily", "2024-02-29"), ("weekly", "2026-W53"), ("weekly", "2025-W01"), ("monthly", "2026-12")],
)
def test_parse_key_roundtrip(ptype: str, key: str) -> None:
    p = parse_period_key(ptype, key)
    assert p.key == key
    assert period_containing(ptype, p.start) == p


@pytest.mark.parametrize(
    ("ptype", "key"),
    [("daily", "2026-3-1"), ("daily", "20260301"), ("weekly", "2021-W53"), ("weekly", "2026-W1"),
     ("monthly", "2026-13"), ("monthly", "2026-00")],
)
def test_parse_invalid_keys(ptype: str, key: str) -> None:
    with pytest.raises(ValueError):
        parse_period_key(ptype, key)


def test_previous_and_next_period() -> None:
    p = period_containing("weekly", dt(2027, 1, 4))
    assert period_before(p).key == "2026-W53"
    assert period_after(period_before(p)) == p


def test_enumerates_multiple_missed_boundaries() -> None:
    installed = dt(2026, 3, 10, 12)
    keys = [p.key for p in ended_periods("daily", installed, dt(2026, 3, 13, 0, 5))]
    assert keys == ["2026-03-10", "2026-03-11", "2026-03-12"]
    assert ended_periods("weekly", installed, dt(2026, 3, 13, 0, 5)) == []
    weekly = ended_periods("weekly", installed, dt(2026, 3, 30, 0, 1))
    assert [p.key for p in weekly] == ["2026-W11", "2026-W12", "2026-W13"]


def test_first_partial_period_included_and_earlier_periods_excluded() -> None:
    installed = dt(2026, 3, 10, 12)
    periods = ended_periods("daily", installed, dt(2026, 3, 11, 0, 0))
    assert [p.key for p in periods] == ["2026-03-10"]  # partial day after install counts
    monthly = ended_periods("monthly", installed, dt(2026, 4, 1))
    assert [p.key for p in monthly] == ["2026-03"]
    # Nothing that ended before installation.
    assert all(p.end > installed for p in ended_periods("daily", installed, dt(2026, 3, 20)))


def test_period_ending_exactly_at_installation_is_excluded() -> None:
    installed = dt(2026, 3, 10)  # exactly the boundary
    keys = [p.key for p in ended_periods("daily", installed, dt(2026, 3, 11, 0, 1))]
    assert keys == ["2026-03-10"]


def test_enumeration_resumes_after_last_recorded_period() -> None:
    installed = dt(2026, 3, 10, 12)
    keys = [p.key for p in ended_periods("daily", installed, dt(2026, 3, 14), after=dt(2026, 3, 12))]
    assert keys == ["2026-03-12", "2026-03-13"]


def test_next_reset_and_unix_timestamps() -> None:
    now = dt(2026, 3, 10, 12)
    assert next_reset("daily", now) == dt(2026, 3, 11)
    assert next_reset("weekly", now) == dt(2026, 3, 16)
    assert next_reset("monthly", now) == dt(2026, 4, 1)
    assert unix_timestamp(dt(2026, 3, 11)) == int(dt(2026, 3, 11).timestamp()) == 1773187200


def test_day_range_and_board_range() -> None:
    week = period_containing("weekly", dt(2026, 3, 10))
    assert day_range(week.start, week.end) == ("2026-03-09", "2026-03-16")
    # A range ending mid-day includes that whole day (forced test periods end at "now").
    assert day_range(dt(2026, 3, 10), dt(2026, 3, 10, 12, 30)) == ("2026-03-10", "2026-03-11")
    assert board_range("alltime", dt(2026, 3, 10)) == (None, None)
    assert board_range("monthly", dt(2026, 3, 10)) == (dt(2026, 3, 1), dt(2026, 4, 1))


def test_period_type_values() -> None:
    assert [t.value for t in PeriodType] == ["daily", "weekly", "monthly"]
