"""Deterministic Schedule recurrence validation and next-run calculation.

Pure module: no DB, no network, no system timezone dependence.
Durable outputs are UTC-aware. Expression evaluation uses Schedule.timezone.

DST policy is owned by MCPFlow (not croniter):
- spring-forward nonexistent wall time → skip candidate
- fall-back ambiguous wall time → fold=0 only (never fold=1)
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
# Schedule v1 CRON field grammar: digits, *, ,, -, / only (no L/#/?/@/names).
_CRON_FIELD_RE = re.compile(r"^[0-9*,\-/]+$")


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
    try:
        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL numeric components overflow.",
            status_code=400,
        ) from exc
    if days == 0 and hours == 0 and minutes == 0 and seconds == 0:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL duration must be positive.",
            status_code=400,
        )
    try:
        return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except OverflowError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL duration overflows timedelta bounds.",
            status_code=400,
        ) from exc


def _local_wall_exists(local_naive: datetime, tz: ZoneInfo) -> bool:
    """Return False for nonexistent spring-forward wall times.

    Localize naive wall clock with fold=0 and round-trip through UTC. If the
    wall clock components change, the local time does not exist.
    """
    if local_naive.tzinfo is not None:
        raise ValueError("expected naive local wall clock")
    probe = local_naive.replace(tzinfo=tz, fold=0)
    back = probe.astimezone(UTC).astimezone(tz)
    return (
        back.fold == 0
        and back.year == probe.year
        and back.month == probe.month
        and back.day == probe.day
        and back.hour == probe.hour
        and back.minute == probe.minute
        and back.second == probe.second
    )


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
    try:
        local_naive = datetime(
            int(match.group("y")),
            int(match.group("m")),
            int(match.group("d")),
            int(match.group("H")),
            int(match.group("M")),
            int(match.group("S")),
        )
    except ValueError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="ONCE expression is not a valid local date/time.",
            status_code=400,
        ) from exc
    if not _local_wall_exists(local_naive, tz):
        raise AppError(
            code="VALIDATION_ERROR",
            message="ONCE local wall time does not exist in timezone.",
            status_code=400,
        )
    # Ambiguous fall-back: fold=0 only (one occurrence).
    return local_naive.replace(tzinfo=tz, fold=0)


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
    for field in fields:
        if not _CRON_FIELD_RE.fullmatch(field):
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "CRON v1 supports only digits, *, ,, -, / "
                    "(no L, #, ?, names, or macros)."
                ),
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


def _next_cron_utc(
    *,
    expression: str,
    tz: ZoneInfo,
    after_utc: datetime,
    start_at: datetime | None,
    end_at: datetime | None,
) -> datetime | None:
    """Compute next CRON occurrence with MCPFlow-owned DST policy.

    croniter receives only a **naive local wall-clock** cursor and returns naive
    candidates. Localization, fold=0, nonexistent skip, and UTC comparison are
    applied here so croniter DST normalization cannot alter Schedule semantics.

    ``after`` is exclusive. ``start_at`` is inclusive. When ``start_at > after``,
    the cron cursor is positioned near ``start_at`` so far-future windows do not
    burn ``_MAX_SCAN`` skipping pre-window ticks.
    """
    # Exclusive croniter base in naive local time.
    if start_at is not None and start_at > after_utc:
        # Inclusive start_at: exclusive cursor just before start_at wall clock.
        bound_local = start_at.astimezone(tz)
        cursor_naive = bound_local.replace(tzinfo=None) - timedelta(microseconds=1)
    else:
        cursor_naive = after_utc.astimezone(tz).replace(tzinfo=None)

    itr = croniter(expression, cursor_naive)
    for _ in range(_MAX_SCAN):
        nxt_naive = itr.get_next(datetime)
        if getattr(nxt_naive, "tzinfo", None) is not None:
            # Defense: never accept croniter timezone policy.
            nxt_naive = nxt_naive.replace(tzinfo=None)

        if not _local_wall_exists(nxt_naive, tz):
            # Spring-forward nonexistent wall time — skip, do not mutate.
            continue

        localized = nxt_naive.replace(tzinfo=tz, fold=0)
        utc = _to_utc(localized)
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
    try:
        if after_utc < anchor:
            candidate = anchor
        else:
            steps = ((after_utc - anchor) // delta) + 1
            candidate = anchor + steps * delta
    except OverflowError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="INTERVAL next-run calculation overflowed.",
            status_code=400,
        ) from exc
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
