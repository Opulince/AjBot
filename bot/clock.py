"""Injectable clock and UTC helpers.

Every piece of time-sensitive code receives a :class:`Clock` instead of calling
``datetime.now()`` directly, so tests can control time precisely. This module is
the only place in the application that reads the system clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

UTC = timezone.utc


class Clock(Protocol):
    """Source of the current time. Implementations must return aware UTC datetimes."""

    def now(self) -> datetime: ...


class SystemClock:
    """Production clock backed by the operating system."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` as an aware UTC datetime.

    Naive datetimes are interpreted as UTC (discord.py always produces aware
    datetimes, so this only matters for hand-built values).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """Canonical, lexicographically sortable ISO-8601 representation used in SQLite."""
    return ensure_utc(dt).isoformat(timespec="microseconds")


def from_iso(value: str) -> datetime:
    """Parse a value previously produced by :func:`to_iso`."""
    return ensure_utc(datetime.fromisoformat(value))
