"""Deterministic Schedule recurrence validation and next-run calculation.

Pure module: no DB, no network, no system timezone dependence.
Durable outputs are UTC-aware. Expression evaluation uses Schedule.timezone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from app.core.errors import AppError
from app.domain.enums import ScheduleType

_MAX_PREVIEW = 50
_MAX_SCAN = 10_000
_ONCE_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})T"
    r"(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})$"
)
_INTERVAL_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?)?$"
)


@dataclass(frozen=True, slots=True)
class ValidatedScheduleExpression:
    schedule_type: str
    expression: str
    timezone: str
    interval_delta: timedelta | None = None
    once_local: datetime | None = None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"Unknown IANA timezone: {name!r}.",
            status_code=400,
        ) from exc


def _require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"{field} must be timezone-aware.",
            status_code=400,
        )
    return value


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _parse_interval(expression: str) -> timedelta:
    raw = expression.strip()
    if not raw or raw == "P" or raw == "PT":
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL expression must be a positive restricted duration.",
            status_code=400,
        )
    if "Y" in raw or "W" in raw or "M" in raw.split("T", 1)[0]:
        # Reject years/months/weeks; months after T are minutes (handled by regex).
        if re.search(r"[YW]", raw) or re.search(r"\d+M", raw.split("T", 1)[0]):
            raise AppError(
                code="VALIDATION_ERROR",
                message="INTERVAL supports only days/hours/minutes/seconds.",
                status_code=400,
            )
    if "." in raw or "," in raw or "-" in raw:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL rejects fractional/negative durations.",
            status_code=400,
        )
    match = _INTERVAL_RE.fullmatch(raw)
    if match is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL expression must match P[nD][T[nH][nM][nS]].",
            status_code=400,
        )
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    if days == 0 and hours == 0 and minutes == 0 and seconds == 0:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL duration must be positive.",
            status_code=400,
        )
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _parse_once_local(expression: str, tz: ZoneInfo) -> datetime:
    raw = expression.strip()
    if raw.endswith("Z") or "+" in raw[10:] or raw.count("-") > 2:
        raise AppError(
            code="VALIDATION_ERROR",
            message="ONCE expression must be local YYYY-MM-DDTHH:MM:SS (no Z/offset).",
            status_code=400,
        )
    match = _ONCE_RE.fullmatch(raw)
    if match is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="ONCE expression must be YYYY-MM-DDTHH:MM:SS.",
            status_code=400,
        )
    local = datetime(
        int(match.group("y")),
        int(match.group("m")),
        int(match.group("d")),
        int(match.group("H")),
        int(match.group("M")),
        int(match.group("S")),
        tzinfo=tz,
    )
    # Ambiguous fall-back: fold=0 only (one occurrence).
    return local.replace(fold=0)


def _validate_cron(expression: str) -> str:
    raw = expression.strip()
    if not raw:
        raise AppError(
            code="VALIDATION_ERROR",
            message="CRON expression must be non-empty.",
            status_code=400,
        )
    if raw.startswith("@"):
        raise AppError(
            code="VALIDATION_ERROR",
            message="CRON macros such as @daily are not supported.",
            status_code=400,
        )
    fields = raw.split()
    if len(fields) != 5:
        raise AppError(
            code="VALIDATION_ERROR",
            message="CRON must be exactly 5 fields (minute hour dom month dow).",
            status_code=400,
        )
    if not croniter.is_valid(raw):
        raise AppError(
            code="VALIDATION_ERROR",
            message="CRON expression is invalid.",
            status_code=400,
        )
    return raw


def validate_schedule_expression(
    schedule_type: str,
    expression: str,
    timezone: str,
) -> ValidatedScheduleExpression:
    """Validate schedule_type/expression/timezone without computing next runs."""
    try:
        stype = ScheduleType(schedule_type)
    except ValueError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"Invalid schedule_type={schedule_type!r}.",
            status_code=400,
        ) from exc
    tz = _zone(timezone)
    if stype == ScheduleType.CRON:
        expr = _validate_cron(expression)
        return ValidatedScheduleExpression(
            schedule_type=stype.value, expression=expr, timezone=timezone
        )
    if stype == ScheduleType.ONCE:
        once_local = _parse_once_local(expression, tz)
        return ValidatedScheduleExpression(
            schedule_type=stype.value,
            expression=expression.strip(),
            timezone=timezone,
            once_local=once_local,
        )
    delta = _parse_interval(expression)
    return ValidatedScheduleExpression(
        schedule_type=stype.value,
        expression=expression.strip(),
        timezone=timezone,
        interval_delta=delta,
    )


def _in_window(
    candidate: datetime,
    *,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if start_at is not None and candidate < start_at:
        return False
    if end_at is not None and candidate >= end_at:
        return False
    return True


def _local_wall_exists(local_naive_or_aware: datetime, tz: ZoneInfo) -> bool:
    """Return False for nonexistent spring-forward wall times."""
    if local_naive_or_aware.tzinfo is None:
        probe = local_naive_or_aware.replace(tzinfo=tz)
    else:
        probe = local_naive_or_aware.astimezone(tz).replace(tzinfo=tz)
    # Round-trip: if zone shifts the wall clock, the local time does not exist.
    back = probe.astimezone(UTC).astimezone(tz)
    return (
        back.year == probe.year
        and back.month == probe.month
        and back.day == probe.day
        and back.hour == probe.hour
        and back.minute == probe.minute
        and back.second == probe.second
    )


def _next_cron_utc(
    *,
    expression: str,
    tz: ZoneInfo,
    after_utc: datetime,
    start_at: datetime | None,
    end_at: datetime | None,
) -> datetime | None:
    cursor_local = after_utc.astimezone(tz)
    # Exclusive cursor: first occurrence strictly after `after`.
    itr = croniter(expression, cursor_local)
    for _ in range(_MAX_SCAN):
        nxt = itr.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=tz)
        else:
            nxt = nxt.astimezone(tz)
        # Ambiguous: fold=0 only.
        nxt = nxt.replace(fold=0)
        if not _local_wall_exists(nxt, tz):
            continue
        utc = _to_utc(nxt)
        if utc <= after_utc:
            continue
        if start_at is not None and utc < start_at:
            continue
        if end_at is not None and utc >= end_at:
            return None
        return utc
    return None


def _next_interval_utc(
    *,
    delta: timedelta,
    after_utc: datetime,
    start_at: datetime | None,
    end_at: datetime | None,
) -> datetime | None:
    # INTERVAL anchor is start_at (required for phase stability).
    if start_at is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL requires start_at as stable anchor.",
            status_code=400,
        )
    anchor = _to_utc(_require_aware(start_at, field="start_at"))
    if after_utc < anchor:
        candidate = anchor
    else:
        steps = ((after_utc - anchor) // delta) + 1
        candidate = anchor + steps * delta
    if end_at is not None and candidate >= end_at:
        return None
    if start_at is not None and candidate < start_at:
        return None
    return candidate


def next_scheduled_at(
    *,
    schedule_type: str,
    expression: str,
    timezone: str,
    after: datetime,
    start_at: datetime | None,
    end_at: datetime | None,
) -> datetime | None:
    """Return the next UTC run strictly after ``after``, or None."""
    validated = validate_schedule_expression(schedule_type, expression, timezone)
    after_utc = _to_utc(_require_aware(after, field="after"))
    start_utc = (
        _to_utc(_require_aware(start_at, field="start_at")) if start_at is not None else None
    )
    end_utc = (
        _to_utc(_require_aware(end_at, field="end_at")) if end_at is not None else None
    )
    tz = _zone(timezone)

    if validated.schedule_type == ScheduleType.ONCE.value:
        assert validated.once_local is not None
        once_utc = _to_utc(validated.once_local.replace(fold=0))
        if once_utc <= after_utc:
            return None
        if not _in_window(once_utc, start_at=start_utc, end_at=end_utc):
            return None
        return once_utc

    if validated.schedule_type == ScheduleType.CRON.value:
        return _next_cron_utc(
            expression=validated.expression,
            tz=tz,
            after_utc=after_utc,
            start_at=start_utc,
            end_at=end_utc,
        )

    assert validated.interval_delta is not None
    return _next_interval_utc(
        delta=validated.interval_delta,
        after_utc=after_utc,
        start_at=start_utc,
        end_at=end_utc,
    )


def preview_scheduled_times(
    *,
    schedule_type: str,
    expression: str,
    timezone: str,
    after: datetime,
    start_at: datetime | None,
    end_at: datetime | None,
    limit: int = 5,
) -> list[datetime]:
    if limit < 1 or limit > _MAX_PREVIEW:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"preview limit must be 1..{_MAX_PREVIEW}.",
            status_code=400,
        )
    cursor = _to_utc(_require_aware(after, field="after"))
    out: list[datetime] = []
    for _ in range(limit):
        nxt = next_scheduled_at(
            schedule_type=schedule_type,
            expression=expression,
            timezone=timezone,
            after=cursor,
            start_at=start_at,
            end_at=end_at,
        )
        if nxt is None:
            break
        out.append(nxt)
        cursor = nxt
    return out
