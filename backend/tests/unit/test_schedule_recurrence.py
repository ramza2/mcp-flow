"""Unit tests for Schedule recurrence validation and next-run calculation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core.errors import AppError
from app.scheduler.recurrence import (
    next_scheduled_at,
    preview_scheduled_times,
    validate_schedule_expression,
)


def _utc(y: int, m: int, d: int, H: int = 0, M: int = 0, S: int = 0) -> datetime:
    return datetime(y, m, d, H, M, S, tzinfo=UTC)


@pytest.mark.parametrize(
    "timezone",
    ["UTC", "Asia/Seoul", "America/New_York", "Europe/London"],
)
def test_validate_timezone_valid_iana(timezone: str) -> None:
    validated = validate_schedule_expression("CRON", "0 9 * * *", timezone)
    assert validated.timezone == timezone


def test_validate_timezone_invalid_raises() -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("CRON", "0 9 * * *", "Not/A/Timezone")
    assert exc.value.code == "VALIDATION_ERROR"
    assert "IANA" in exc.value.message


def test_cron_weekday_0900_deterministic_asia_seoul() -> None:
    """Monday 2026-01-05 UTC → next weekday 09:00 KST is Tuesday 00:00 UTC."""
    after = _utc(2026, 1, 5, 0, 0, 0)
    nxt = next_scheduled_at(
        schedule_type="CRON",
        expression="0 9 * * 1-5",
        timezone="Asia/Seoul",
        after=after,
        start_at=None,
        end_at=None,
    )
    assert nxt == _utc(2026, 1, 6, 0, 0, 0)


def test_cron_rejects_four_fields() -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("CRON", "0 9 * *", "UTC")
    assert "5 fields" in exc.value.message


def test_cron_rejects_six_fields() -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("CRON", "0 9 * * * *", "UTC")
    assert "5 fields" in exc.value.message


def test_cron_rejects_at_daily_macro() -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("CRON", "@daily", "UTC")
    assert "@daily" in exc.value.message


def test_cron_rejects_invalid_five_field_expression() -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("CRON", "99 99 99 99 99", "UTC")
    assert exc.value.code == "VALIDATION_ERROR"


def test_once_local_converts_to_utc() -> None:
    validated = validate_schedule_expression(
        "ONCE", "2026-07-15T09:00:00", "Asia/Seoul"
    )
    assert validated.once_local is not None
    once_utc = validated.once_local.astimezone(UTC)
    assert once_utc == _utc(2026, 7, 15, 0, 0, 0)


@pytest.mark.parametrize(
    "expression",
    [
        "2026-07-15T09:00:00Z",
        "2026-07-15T09:00:00+09:00",
        "2026-07-15T09:00:00-05:00",
    ],
)
def test_once_rejects_z_or_offset(expression: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("ONCE", expression, "UTC")
    assert "no Z/offset" in exc.value.message


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("PT15M", timedelta(minutes=15)),
        ("PT1H", timedelta(hours=1)),
        ("P1D", timedelta(days=1)),
        ("P1DT2H30M", timedelta(days=1, hours=2, minutes=30)),
    ],
)
def test_interval_valid_durations(expression: str, expected: timedelta) -> None:
    validated = validate_schedule_expression("INTERVAL", expression, "UTC")
    assert validated.interval_delta == expected


@pytest.mark.parametrize(
    "expression",
    ["PT0S", "PT0M", "P0D", "PT", "P"],
)
def test_interval_rejects_zero_duration(expression: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("INTERVAL", expression, "UTC")
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "expression",
    ["P-1D", "PT-1H", "P1.5D", "P1,5D"],
)
def test_interval_rejects_negative_or_fractional(expression: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("INTERVAL", expression, "UTC")
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("expression", ["P1Y", "P1M", "P1W", "P2W"])
def test_interval_rejects_year_month_week(expression: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("INTERVAL", expression, "UTC")
    assert "days/hours/minutes/seconds" in exc.value.message


def test_ny_spring_forward_skips_nonexistent_wall_time() -> None:
    """2:30 AM on US spring-forward day does not exist; skip to next day 02:30 EDT."""
    after = _utc(2026, 3, 8, 5, 0, 0)
    nxt = next_scheduled_at(
        schedule_type="CRON",
        expression="30 2 * * *",
        timezone="America/New_York",
        after=after,
        start_at=None,
        end_at=None,
    )
    # 2026-03-09 02:30 America/New_York (EDT, UTC-4) → 06:30Z
    assert nxt == _utc(2026, 3, 9, 6, 30, 0)
    # Must not mutate to same-day 03:00 / 03:30.
    assert nxt != _utc(2026, 3, 8, 7, 0, 0)
    assert nxt != _utc(2026, 3, 8, 7, 30, 0)


def test_ny_fall_back_fold_zero_single_occurrence_in_preview() -> None:
    """1:30 AM happens twice on fall-back; fold=0 yields one occurrence that day."""
    after = _utc(2026, 11, 1, 4, 0, 0)
    times = preview_scheduled_times(
        schedule_type="CRON",
        expression="30 1 * * *",
        timezone="America/New_York",
        after=after,
        start_at=None,
        end_at=None,
        limit=10,
    )
    nov1 = [t for t in times if t.date() == datetime(2026, 11, 1, tzinfo=UTC).date()]
    assert len(nov1) == 1
    assert nov1[0] == _utc(2026, 11, 1, 5, 30, 0)
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    local = nov1[0].astimezone(ny)
    assert local.hour == 1 and local.minute == 30
    assert local.fold == 0
    # fold=1 duplicate must never appear.
    assert all(t.astimezone(ny).fold == 0 for t in times)


def test_ny_fall_back_second_fold_cursor_skips_prior_fold0() -> None:
    """Cursor inside second fold must not emit first-fold 01:30 or fold=1."""
    # 2026-11-01T06:15:00Z == local second-fold 01:15 EST.
    after = _utc(2026, 11, 1, 6, 15, 0)
    nxt = next_scheduled_at(
        schedule_type="CRON",
        expression="30 1 * * *",
        timezone="America/New_York",
        after=after,
        start_at=None,
        end_at=None,
    )
    assert nxt == _utc(2026, 11, 2, 6, 30, 0)
    assert nxt != _utc(2026, 11, 1, 5, 30, 0)
    from zoneinfo import ZoneInfo

    local = nxt.astimezone(ZoneInfo("America/New_York"))
    assert local.fold == 0


def test_once_spring_forward_nonexistent_rejected() -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression(
            "ONCE", "2026-03-08T02:30:00", "America/New_York"
        )
    assert exc.value.code == "VALIDATION_ERROR"
    assert "does not exist" in exc.value.message


def test_once_fall_back_uses_fold_zero() -> None:
    validated = validate_schedule_expression(
        "ONCE", "2026-11-01T01:30:00", "America/New_York"
    )
    assert validated.once_local is not None
    assert validated.once_local.fold == 0
    assert validated.once_local.astimezone(UTC) == _utc(2026, 11, 1, 5, 30, 0)


@pytest.mark.parametrize(
    "expression",
    [
        "2026-13-01T09:00:00",
        "2026-02-30T09:00:00",
        "2026-01-01T25:00:00",
        "2026-01-01T09:61:00",
    ],
)
def test_once_invalid_calendar_values_are_validation_errors(expression: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("ONCE", expression, "UTC")
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.status_code == 400


def test_interval_overflow_is_validation_error() -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("INTERVAL", "P999999999999999999D", "UTC")
    assert exc.value.code == "VALIDATION_ERROR"
    assert "overflow" in exc.value.message.lower()


@pytest.mark.parametrize("expression", ["0 9 * * L", "0 9 * * 1#1", "0 9 * * ?", "0 9 * MON *"])
def test_cron_rejects_extension_syntax(expression: str) -> None:
    with pytest.raises(AppError) as exc:
        validate_schedule_expression("CRON", expression, "UTC")
    assert exc.value.code == "VALIDATION_ERROR"


def test_cron_far_future_start_at_does_not_exhaust_scan() -> None:
    after = _utc(2026, 1, 1, 0, 0, 0)
    start_at = _utc(2026, 3, 1, 0, 0, 0)
    nxt = next_scheduled_at(
        schedule_type="CRON",
        expression="* * * * *",
        timezone="UTC",
        after=after,
        start_at=start_at,
        end_at=None,
    )
    assert nxt == start_at


def test_cron_far_future_start_beyond_ten_thousand_ticks() -> None:
    """More than 10_000 minute ticks before start_at must still resolve."""
    after = _utc(2026, 1, 1, 0, 0, 0)
    start_at = after + timedelta(days=30)
    nxt = next_scheduled_at(
        schedule_type="CRON",
        expression="* * * * *",
        timezone="UTC",
        after=after,
        start_at=start_at,
        end_at=None,
    )
    assert nxt == start_at
    assert (start_at - after).total_seconds() / 60 > 10_000


def test_start_inclusive_end_exclusive_window() -> None:
    start = _utc(2026, 1, 10, 9, 0, 0)
    end = _utc(2026, 1, 11, 9, 0, 0)
    after = _utc(2026, 1, 9, 0, 0, 0)
    nxt = next_scheduled_at(
        schedule_type="CRON",
        expression="0 9 * * *",
        timezone="UTC",
        after=after,
        start_at=start,
        end_at=end,
    )
    assert nxt == start
    after_start = _utc(2026, 1, 10, 9, 0, 1)
    assert (
        next_scheduled_at(
            schedule_type="CRON",
            expression="0 9 * * *",
            timezone="UTC",
            after=after_start,
            start_at=start,
            end_at=end,
        )
        is None
    )


def test_preview_limit_bounded() -> None:
    after = _utc(2026, 1, 1, 0, 0, 0)
    times = preview_scheduled_times(
        schedule_type="CRON",
        expression="0 * * * *",
        timezone="UTC",
        after=after,
        start_at=None,
        end_at=None,
        limit=3,
    )
    assert len(times) == 3
    with pytest.raises(AppError) as exc:
        preview_scheduled_times(
            schedule_type="CRON",
            expression="0 * * * *",
            timezone="UTC",
            after=after,
            start_at=None,
            end_at=None,
            limit=51,
        )
    assert "preview limit" in exc.value.message


def test_asia_seoul_no_dst_smoke() -> None:
    """Seoul has no DST; hourly cron advances predictably across 'DST season'."""
    after = _utc(2026, 3, 1, 0, 0, 0)
    times = preview_scheduled_times(
        schedule_type="CRON",
        expression="0 * * * *",
        timezone="Asia/Seoul",
        after=after,
        start_at=None,
        end_at=None,
        limit=5,
    )
    assert len(times) == 5
    for i in range(1, len(times)):
        assert times[i] - times[i - 1] == timedelta(hours=1)


def test_next_requires_timezone_aware_after() -> None:
    with pytest.raises(AppError) as exc:
        next_scheduled_at(
            schedule_type="CRON",
            expression="0 9 * * *",
            timezone="UTC",
            after=datetime(2026, 1, 1, 0, 0, 0),
            start_at=None,
            end_at=None,
        )
    assert "timezone-aware" in exc.value.message
