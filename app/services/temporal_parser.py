from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
_CLOCK_HOUR = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
_POINT_CLOCK_PATTERN = re.compile(
    rf"^(早上|上午|中午|下午|晚上)?({_CLOCK_HOUR})点(?:钟|(半)|(\d{{1,2}})分?)?$"
)
_COLON_CLOCK_PATTERN = re.compile(
    r"^(早上|上午|中午|下午|晚上)?(\d{1,2})[:：](\d{2})$"
)
_CHINESE_CLOCK_HOURS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十三": 13,
    "十四": 14,
    "十五": 15,
    "十六": 16,
    "十七": 17,
    "十八": 18,
    "十九": 19,
    "二十": 20,
    "二十一": 21,
    "二十二": 22,
    "二十三": 23,
}


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


@dataclass(frozen=True, slots=True)
class ParsedClock:
    local_time: time
    day_offset: int = 0
    daypart: str | None = None


def _valid_utc_occurrences(
    local_candidate: datetime,
    zone: ZoneInfo,
) -> tuple[datetime, ...]:
    occurrences = {
        local_candidate.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        for fold in (0, 1)
    }
    return tuple(
        sorted(
            occurrence.replace(microsecond=0)
            for occurrence in occurrences
            if occurrence.astimezone(zone).replace(tzinfo=None) == local_candidate
        )
    )


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

        time_only = self._parse_clock(expression, default_reminder_time)
        if time_only is not None:
            return self._resolve_time_only(
                time_only,
                local_reference=local_reference,
                timezone_name=timezone_name,
                reference_utc=reference_utc,
            )

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
    def _resolve_time_only(
        parsed_clock: ParsedClock,
        *,
        local_reference: datetime,
        timezone_name: str,
        reference_utc: datetime,
    ) -> TemporalParseResult:
        clocks = [parsed_clock.local_time]
        if parsed_clock.daypart is None:
            hour = parsed_clock.local_time.hour
            minute = parsed_clock.local_time.minute
            if 1 <= hour <= 11:
                clocks.append(time(hour=hour + 12, minute=minute))
            elif hour == 12:
                clocks = [time(hour=0, minute=minute), parsed_clock.local_time]
        clocks.sort()

        first_date = local_reference.date() + timedelta(
            days=parsed_clock.day_offset
        )
        candidates = [
            datetime.combine(first_date + timedelta(days=day_offset), clock)
            for day_offset in range(2)
            for clock in clocks
        ]
        zone = ZoneInfo(timezone_name)
        reference_wall_time = local_reference.replace(tzinfo=None)
        for candidate in candidates:
            occurrences = _valid_utc_occurrences(candidate, zone)
            if not occurrences:
                if candidate > reference_wall_time:
                    return TemporalParseResult.unresolved(
                        "nonexistent_local_time"
                    )
                continue

            future_occurrences = tuple(
                occurrence
                for occurrence in occurrences
                if occurrence > reference_utc
            )
            if len(occurrences) > 1:
                if future_occurrences:
                    return TemporalParseResult.unresolved(
                        "ambiguous_local_time"
                    )
                continue
            if future_occurrences:
                return TemporalParseResult(remind_at=future_occurrences[0])

        return TemporalParseResult.unresolved("resolved_time_is_not_future")

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
        local_date += timedelta(days=parsed_clock.day_offset)
        local_clock = parsed_clock.local_time
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
    ) -> ParsedClock | None:
        if not expression:
            return ParsedClock(time.fromisoformat(default_reminder_time))

        point_match = _POINT_CLOCK_PATTERN.fullmatch(expression)
        colon_match = _COLON_CLOCK_PATTERN.fullmatch(expression)
        if point_match is not None:
            daypart, hour_text, half, minute_text = point_match.groups()
            minute = 30 if half else int(minute_text or 0)
        elif colon_match is not None:
            daypart, hour_text, minute_text = colon_match.groups()
            minute = int(minute_text)
        else:
            return None

        hour = (
            int(hour_text)
            if hour_text.isascii() and hour_text.isdigit()
            else _CHINESE_CLOCK_HOURS.get(hour_text)
        )
        if hour is None:
            return None
        if minute > 59:
            return None

        day_offset = 0
        if daypart in {"早上", "上午"}:
            if hour > 12:
                return None
            hour = 0 if hour == 12 else hour
        elif daypart == "中午":
            if hour != 12:
                return None
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

        return ParsedClock(
            local_time=time(hour=hour, minute=minute),
            day_offset=day_offset,
            daypart=daypart,
        )
