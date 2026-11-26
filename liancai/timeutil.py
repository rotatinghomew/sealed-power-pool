"""Timezone aware date/time helpers.

All persisted timestamps are ISO-8601 strings with an explicit UTC offset.
Naive values are rejected: in a procurement deadline a naive timestamp is an
ambiguity, not a convenience.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from .errors import ValidationError

TZ = timezone(timedelta(hours=8))


def parse_dt(value: str | datetime, *, field: str = "time") -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 不是合法的 ISO-8601 时间", field=field) from exc
    else:
        raise ValidationError(f"{field} 必须是 ISO-8601 时间", field=field)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationError(f"{field} 必须带时区偏移", field=field)
    return dt


def format_dt(value: datetime) -> str:
    dt = parse_dt(value)
    # Keep seconds resolution; sub-second differences are not meaningful for
    # receipt/deadline ordering and would make audit output noisy.
    return dt.replace(microsecond=0).isoformat()


class Clock(Protocol):
    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(TZ).replace(microsecond=0)


class FixedClock:
    """Deterministic clock used by tests and by reproducible demo runs."""

    def __init__(self, value: str | datetime):
        self._value = parse_dt(value, field="clock")

    def now(self) -> datetime:
        return self._value

    def advance(self, **kwargs) -> "FixedClock":
        self._value = self._value + timedelta(**kwargs)
        return self