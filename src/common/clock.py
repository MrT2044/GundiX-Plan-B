"""Time source.

Every component takes a :class:`Clock` instead of calling ``datetime.now`` directly, so
expiry, staleness and latency behaviour are testable without sleeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a UTC-aware datetime."""


class SystemClock:
    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Manually advanced clock for tests and deterministic replay."""

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock needs a UTC-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FrozenClock needs a UTC-aware datetime")
        self._now = moment.astimezone(UTC)


def millis_between(start: datetime, end: datetime) -> int:
    """Whole milliseconds between two instants. Negative values are preserved."""
    return round((end - start).total_seconds() * 1000)
