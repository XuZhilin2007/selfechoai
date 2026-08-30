from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.temporal_parser import TemporalParser


UTC = timezone.utc
REFERENCE = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)


def parse(
    expression: str | None,
    *,
    timezone_name: str = "Asia/Shanghai",
    default_time: str = "09:00",
    now_utc: datetime = REFERENCE,
):
    return TemporalParser().parse(
        expression,
        timezone_name=timezone_name,
        default_reminder_time=default_time,
        now_utc=now_utc,
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("15分钟后", datetime(2026, 8, 27, 2, 15, tzinfo=UTC)),
        ("30 分钟后", datetime(2026, 8, 27, 2, 30, tzinfo=UTC)),
        ("半小时后", datetime(2026, 8, 27, 2, 30, tzinfo=UTC)),
        ("1小时后", datetime(2026, 8, 27, 3, 0, tzinfo=UTC)),
        ("2小时后", datetime(2026, 8, 27, 4, 0, tzinfo=UTC)),
    ],
)
def test_relative_duration_uses_absolute_utc_duration(expression, expected):
    result = parse(expression)

    assert result.remind_at == expected
    assert not result.needs_confirmation


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ("2分钟", datetime(2026, 8, 27, 2, 2, tzinfo=UTC)),
        ("1小时", datetime(2026, 8, 27, 3, 0, tzinfo=UTC)),
        ("半小时", datetime(2026, 8, 27, 2, 30, tzinfo=UTC)),
    ],
)
def test_relative_duration_suffixes_resolve_to_the_same_instant(duration, expected):
    results = [parse(f"{duration}{suffix}") for suffix in ("后", "之后", "以后")]

    assert {result.remind_at for result in results} == {expected}
    assert all(not result.needs_confirmation for result in results)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("明天", datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
        ("后天", datetime(2026, 8, 29, 1, 0, tzinfo=UTC)),
        ("大后天", datetime(2026, 8, 30, 1, 0, tzinfo=UTC)),
        ("明天9点", datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
        ("明天上午9点", datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
        ("明天下午3点", datetime(2026, 8, 28, 7, 0, tzinfo=UTC)),
        ("后天10点", datetime(2026, 8, 29, 2, 0, tzinfo=UTC)),
        ("后天晚上8点", datetime(2026, 8, 29, 12, 0, tzinfo=UTC)),
        ("8月30日", datetime(2026, 8, 30, 1, 0, tzinfo=UTC)),
        ("8月30号", datetime(2026, 8, 30, 1, 0, tzinfo=UTC)),
        ("8月30日上午9点", datetime(2026, 8, 30, 1, 0, tzinfo=UTC)),
        ("8月30日下午3点", datetime(2026, 8, 30, 7, 0, tzinfo=UTC)),
        ("8月30日15点", datetime(2026, 8, 30, 7, 0, tzinfo=UTC)),
        ("8月30日晚上8点", datetime(2026, 8, 30, 12, 0, tzinfo=UTC)),
    ],
)
def test_calendar_expressions_resolve_in_user_timezone(expression, expected):
    assert parse(expression).remind_at == expected


def test_date_only_uses_non_default_user_setting():
    result = parse("明天", default_time="07:45")

    assert result.remind_at == datetime(2026, 8, 27, 23, 45, tzinfo=UTC)


def test_yearless_month_day_rolls_to_next_future_year():
    result = parse(
        "8月30日",
        now_utc=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )

    assert result.remind_at == datetime(2027, 8, 30, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("timezone_name", "expected"),
    [
        ("Asia/Shanghai", datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
        ("America/New_York", datetime(2026, 8, 28, 13, 0, tzinfo=UTC)),
        ("Asia/Tokyo", datetime(2026, 8, 28, 0, 0, tzinfo=UTC)),
    ],
)
def test_calendar_date_uses_each_profile_timezone(timezone_name, expected):
    result = parse(
        "明天",
        timezone_name=timezone_name,
        now_utc=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
    )

    assert result.remind_at == expected


def test_daypart_noon_and_midnight_mappings_are_explicit():
    assert parse("明天下午12点").remind_at == datetime(
        2026, 8, 28, 4, 0, tzinfo=UTC
    )
    assert parse("明天晚上12点").remind_at == datetime(
        2026, 8, 28, 16, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("2026年3月8日2点", "nonexistent_local_time"),
        ("2026年11月1日1点30分", "ambiguous_local_time"),
    ],
)
def test_dst_gap_and_fold_are_not_guessed(expression, reason):
    result = parse(
        expression,
        timezone_name="America/New_York",
        now_utc=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )

    assert result.needs_confirmation
    assert result.unresolved_reason == reason


@pytest.mark.parametrize(
    "expression",
    [
        None,
        "",
        "过几天",
        "最近",
        "过阵子",
        "之后",
        "之后提醒我",
        "月底找个时间",
        "有空时",
        "傍晚",
    ],
)
def test_ambiguous_or_missing_expressions_never_produce_time(expression):
    result = parse(expression)

    assert result.needs_confirmation
    assert result.remind_at is None


def test_explicit_past_year_is_not_silently_changed():
    result = parse("2025年8月30日")

    assert result.needs_confirmation
    assert result.unresolved_reason == "resolved_time_is_not_future"
