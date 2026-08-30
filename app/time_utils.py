from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_REMINDER_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate_timezone_name(value: str) -> str:
    timezone_name = value.strip()
    if not timezone_name:
        raise ValueError("timezone must not be blank")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return timezone_name


def validate_default_reminder_time(value: str) -> str:
    reminder_time = value.strip()
    if not _REMINDER_TIME_PATTERN.fullmatch(reminder_time):
        raise ValueError("default reminder time must use HH:MM 24-hour format")
    return reminder_time


def local_datetime_to_utc(
    local_date: date,
    local_time: str,
    timezone_name: str,
) -> datetime:
    """Resolve one unambiguous local wall-clock time to canonical UTC."""

    timezone_name = validate_timezone_name(timezone_name)
    local_time = validate_default_reminder_time(local_time)
    zone = ZoneInfo(timezone_name)
    local_naive = datetime.combine(local_date, time.fromisoformat(local_time))
    candidates = [
        local_naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        for fold in (0, 1)
    ]
    valid_candidates = [
        candidate
        for candidate in candidates
        if candidate.astimezone(zone).replace(tzinfo=None) == local_naive
    ]
    if not valid_candidates:
        raise ValueError("selected local time does not exist in the user timezone")
    if len(set(valid_candidates)) > 1:
        raise ValueError("selected local time is ambiguous in the user timezone")
    return valid_candidates[0].replace(microsecond=0)


def normalize_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def serialize_utc_datetime(value: datetime, *, field_name: str) -> str:
    return normalize_utc_datetime(value, field_name=field_name).isoformat(
        timespec="seconds"
    )
