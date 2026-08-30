from __future__ import annotations

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
