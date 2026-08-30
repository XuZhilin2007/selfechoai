from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone_name(value: str) -> str:
    timezone_name = value.strip()
    if not timezone_name:
        raise ValueError("timezone must not be blank")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return timezone_name


def normalize_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def serialize_utc_datetime(value: datetime, *, field_name: str) -> str:
    return normalize_utc_datetime(value, field_name=field_name).isoformat(
        timespec="seconds"
    )
