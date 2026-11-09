"""Unambiguous UTC timestamps and half-open windows."""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import InvalidTimeError
from .types import TimestampInput


def utc(value: TimestampInput, zone: str | None = None) -> datetime:
    try:
        if isinstance(value, str):
            value = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
        if isinstance(value, date) and not isinstance(value, datetime):
            if zone is None:
                raise ValueError("Date-only inputs require a declared dataset timezone")
            value = datetime.combine(value, time())
        if value.tzinfo is None:
            if zone is None:
                raise ValueError("Naive timestamps require a declared timezone")
            tz = ZoneInfo(zone)
            possibilities = {
                value.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc) for fold in (0, 1)
            }
            valid = [v for v in possibilities if v.astimezone(tz).replace(tzinfo=None) == value]
            if len(valid) != 1:
                raise ValueError("Ambiguous or nonexistent local time; provide an explicit offset")
            return valid[0]
        # Explicit ZoneInfo values can still represent nonexistent wall-clock times.
        converted = value.astimezone(timezone.utc)
        if converted.astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(tzinfo=None):
            raise ValueError("Nonexistent local time")
        return converted
    except (ValueError, TypeError, AttributeError, ZoneInfoNotFoundError) as exc:
        raise InvalidTimeError(f"Invalid timestamp {value!r}: {exc}") from exc
