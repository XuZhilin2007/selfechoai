from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.time_utils import (
    local_datetime_to_utc,
    normalize_utc_datetime,
    validate_default_reminder_time,
    validate_timezone_name,
)


_DURATION_PATTERN = re.compile(r"^(\d+)(分钟|小时)(?:后|之后|以后)$")
_RELATIVE_DATE_PATTERN = re.compile(r"^(明天|后天|大后天)(.*)$")
_EXPLICIT_DATE_PATTERN = re.compile(
    r"^(?:(\d{4})年)?(\d{1,2})月(\d{1,2})(?:日|号)(.*)$"
)
_CLOCK_PATTERN = re.compile(
    r"^(早上|上午|下午|晚上)?(\d{1,2})点(?:(半)|(\d{1,2})分?)?$"
)


@dataclass(frozen=True, slots=True)
class TemporalParseResult:
    remind_at: datetime | None
    unresolved_reason: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.remind_at is None

    @classmethod
    def unresolved(cls, reason: str) -> TemporalParseResult:
        return cls(remind_at=None, unresolved_reason=reason)


class TemporalParser:
    """Resolve the deliberately small frozen temporal-expression grammar."""

    def parse(
        self,
        temporal_expression: str | None,
        *,
        timezone_name: str,
        default_reminder_time: str,
        now_utc: datetime,
    ) -> TemporalParseResult:
        timezone_name = validate_timezone_name(timezone_name)
        default_reminder_time = validate_default_reminder_time(
            default_reminder_time
        )
        reference_utc = normalize_utc_datetime(now_utc, field_name="now_utc")
        if temporal_expression is None or not temporal_expression.strip():
            return TemporalParseResult.unresolved("missing_expression")

        expression = re.sub(r"\s+", "", temporal_expression.strip())
        duration = self._parse_duration(expression, reference_utc)
        if duration is not None:
            return duration

        zone = ZoneInfo(timezone_name)
        local_reference = reference_utc.astimezone(zone)

        relative = _RELATIVE_DATE_PATTERN.fullmatch(expression)
        if relative:
            days = {"明天": 1, "后天": 2, "大后天": 3}[relative.group(1)]
            return self._resolve_calendar_time(
                local_reference.date() + timedelta(days=days),
                relative.group(2),
                timezone_name=timezone_name,
                default_reminder_time=default_reminder_time,
                reference_utc=reference_utc,
                allow_year_rollover=False,
            )

        explicit = _EXPLICIT_DATE_PATTERN.fullmatch(expression)
        if explicit:
            year_text, month_text, day_text, clock_text = explicit.groups()
            month = int(month_text)
            day_value = int(day_text)
            inferred_year = year_text is None
            year = int(year_text) if year_text else local_reference.year
            if inferred_year and (month, day_value) < (
                local_reference.month,
                local_reference.day,
            ):
                year += 1
            try:
                local_date = date(year, month, day_value)
            except ValueError:
                return TemporalParseResult.unresolved("invalid_calendar_date")
            return self._resolve_calendar_time(
                local_date,
                clock_text,
                timezone_name=timezone_name,
                default_reminder_time=default_reminder_time,
                reference_utc=reference_utc,
                allow_year_rollover=inferred_year,
            )

        return TemporalParseResult.unresolved("unsupported_or_ambiguous_expression")

    @staticmethod
    def _parse_duration(
        expression: str,
        reference_utc: datetime,
    ) -> TemporalParseResult | None:
        if expression in {"半小时后", "半小时之后", "半小时以后"}:
            return TemporalParseResult(reference_utc + timedelta(minutes=30))
        match = _DURATION_PATTERN.fullmatch(expression)
        if match is None:
            return None
        amount = int(match.group(1))
        if amount <= 0:
            return TemporalParseResult.unresolved("duration_must_be_positive")
        try:
            delta = (
                timedelta(minutes=amount)
                if match.group(2) == "分钟"
                else timedelta(hours=amount)
            )
            remind_at = reference_utc + delta
        except OverflowError:
            return TemporalParseResult.unresolved("duration_out_of_range")
        return TemporalParseResult(remind_at)

    def _resolve_calendar_time(
        self,
        local_date: date,
        clock_expression: str,
        *,
        timezone_name: str,
        default_reminder_time: str,
        reference_utc: datetime,
        allow_year_rollover: bool,
    ) -> TemporalParseResult:
        parsed_clock = self._parse_clock(clock_expression, default_reminder_time)
        if parsed_clock is None:
            return TemporalParseResult.unresolved("unsupported_or_ambiguous_time")
        local_clock, day_offset = parsed_clock
        local_date += timedelta(days=day_offset)
        local_time = local_clock.isoformat(timespec="minutes")
        try:
            remind_at = local_datetime_to_utc(
                local_date,
                local_time,
                timezone_name,
            )
        except ValueError as exc:
            reason = (
                "ambiguous_local_time"
                if "ambiguous" in str(exc)
                else "nonexistent_local_time"
            )
            return TemporalParseResult.unresolved(reason)

        if remind_at > reference_utc:
            return TemporalParseResult(remind_at=remind_at)
        if not allow_year_rollover:
            return TemporalParseResult.unresolved("resolved_time_is_not_future")

        try:
            next_year_date = local_date.replace(year=local_date.year + 1)
            next_year = local_datetime_to_utc(
                next_year_date,
                local_time,
                timezone_name,
            )
        except ValueError:
            return TemporalParseResult.unresolved("invalid_calendar_date")
        return TemporalParseResult(remind_at=next_year)

    @staticmethod
    def _parse_clock(
        expression: str,
        default_reminder_time: str,
    ) -> tuple[time, int] | None:
        if not expression:
            return time.fromisoformat(default_reminder_time), 0
        match = _CLOCK_PATTERN.fullmatch(expression)
        if match is None:
            return None

        daypart, hour_text, half, minute_text = match.groups()
        hour = int(hour_text)
        minute = 30 if half else int(minute_text or 0)
        if minute > 59:
            return None

        day_offset = 0
        if daypart in {"早上", "上午"}:
            if hour > 12:
                return None
            hour = 0 if hour == 12 else hour
        elif daypart == "下午":
            if hour < 1 or hour > 12:
                return None
            hour = 12 if hour == 12 else hour + 12
        elif daypart == "晚上":
            if hour == 12:
                hour = 0
                day_offset = 1
            elif 6 <= hour <= 11:
                hour += 12
            else:
                return None
        elif hour > 23:
            return None

        return time(hour=hour, minute=minute), day_offset
