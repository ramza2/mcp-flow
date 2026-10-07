"""Read-only operations aggregates for Dashboard / stats / system-health."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ApprovalStatus,
    ExecutionStatus,
    MCPServerStatus,
    MCPToolStatus,
    ScheduleStatus,
)
from app.models.approval import ApprovalRequest
from app.models.execution import Execution
from app.models.mcp import MCPServer, MCPTool
from app.models.outbox import OutboxEvent
from app.models.schedule import Schedule
from app.ops.error_category import classify_error_category

_TERMINAL = frozenset(
    {
        ExecutionStatus.SUCCEEDED.value,
        ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.CANCELLED.value,
        ExecutionStatus.TIMED_OUT.value,
    }
)


class OperationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def execution_status_counts(
        self, *, from_time: datetime, to_time: datetime
    ) -> dict[str, int]:
        stmt = (
            select(Execution.status, func.count())
            .where(
                Execution.requested_at >= from_time,
                Execution.requested_at < to_time,
            )
            .group_by(Execution.status)
        )
        rows = (await self._session.execute(stmt)).all()
        return {str(status): int(count) for status, count in rows}

    async def duration_aggregates(
        self, *, from_time: datetime, to_time: datetime
    ) -> dict[str, float | None]:
        """avg / p50 / p95 / max for terminal Executions with valid timestamps."""
        duration_seconds = func.extract(
            "epoch", Execution.finished_at - Execution.started_at
        )
        duration_ms = duration_seconds * 1000.0
        where = and_duration_window(from_time, to_time)

        dialect = self._session.bind.dialect.name if self._session.bind else "sqlite"
        if dialect == "postgresql":
            stmt = select(
                func.avg(duration_ms),
                func.percentile_cont(0.50).within_group(duration_ms),
                func.percentile_cont(0.95).within_group(duration_ms),
                func.max(duration_ms),
            ).where(*where)
            row = (await self._session.execute(stmt)).one()
            return {
                "avg_ms": _f(row[0]),
                "p50_ms": _f(row[1]),
                "p95_ms": _f(row[2]),
                "max_ms": _f(row[3]),
            }

        # SQLite fallback for unit tests — compute in Python over bounded set.
        stmt = select(duration_ms).where(*where)
        values = [float(v) for (v,) in (await self._session.execute(stmt)).all() if v is not None]
        if not values:
            return {"avg_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
        values.sort()
        return {
            "avg_ms": sum(values) / len(values),
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "max_ms": values[-1],
        }

    async def recent_executions(
        self, *, limit: int, from_time: datetime | None = None, to_time: datetime | None = None
    ) -> list[Execution]:
        stmt = select(Execution)
        if from_time is not None:
            stmt = stmt.where(Execution.requested_at >= from_time)
        if to_time is not None:
            stmt = stmt.where(Execution.requested_at < to_time)
        stmt = stmt.order_by(Execution.requested_at.desc(), Execution.id.desc()).limit(
            limit
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def approval_counts(self, *, now: datetime) -> tuple[int, int]:
        pending = (
            await self._session.execute(
                select(func.count()).where(
                    ApprovalRequest.status == ApprovalStatus.PENDING.value
                )
            )
        ).scalar_one()
        overdue = (
            await self._session.execute(
                select(func.count()).where(
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                    ApprovalRequest.expires_at <= now,
                )
            )
        ).scalar_one()
        return int(pending), int(overdue)

    async def schedule_counts(
        self, *, now: datetime, misfire_grace_seconds: int
    ) -> dict[str, int]:
        grace_boundary = now - timedelta(seconds=misfire_grace_seconds)
        base = Schedule.deleted_at.is_(None)
        by_status = (
            await self._session.execute(
                select(Schedule.status, func.count())
                .where(base)
                .group_by(Schedule.status)
            )
        ).all()
        counts = {str(s): int(c) for s, c in by_status}
        overdue = (
            await self._session.execute(
                select(func.count()).where(
                    base,
                    Schedule.status == ScheduleStatus.ACTIVE.value,
                    Schedule.next_run_at.is_not(None),
                    Schedule.next_run_at < grace_boundary,
                )
            )
        ).scalar_one()
        return {
            "active": counts.get(ScheduleStatus.ACTIVE.value, 0),
            "paused": counts.get(ScheduleStatus.PAUSED.value, 0),
            "completed": counts.get(ScheduleStatus.COMPLETED.value, 0),
            "error": counts.get(ScheduleStatus.ERROR.value, 0),
            "overdue": int(overdue),
        }

    async def mcp_server_counts(self) -> dict[str, int]:
        rows = (
            await self._session.execute(
                select(MCPServer.status, func.count())
                .where(MCPServer.deleted_at.is_(None))
                .group_by(MCPServer.status)
            )
        ).all()
        counts = {str(s): int(c) for s, c in rows}
        total = sum(counts.values())
        return {
            "total": total,
            "active": counts.get(MCPServerStatus.ACTIVE.value, 0),
            "inactive": counts.get(MCPServerStatus.INACTIVE.value, 0),
            "error": counts.get(MCPServerStatus.ERROR.value, 0),
            "draft": counts.get(MCPServerStatus.DRAFT.value, 0),
        }

    async def mcp_tool_counts(self) -> dict[str, int]:
        rows = (
            await self._session.execute(
                select(MCPTool.status, func.count())
                .where(MCPTool.deleted_at.is_(None))
                .group_by(MCPTool.status)
            )
        ).all()
        counts = {str(s): int(c) for s, c in rows}
        missing = counts.get(MCPToolStatus.MISSING.value, 0)
        blocked = counts.get(MCPToolStatus.BLOCKED.value, 0)
        return {
            "total": sum(counts.values()),
            "discovered": counts.get(MCPToolStatus.DISCOVERED.value, 0),
            "active": counts.get(MCPToolStatus.ACTIVE.value, 0),
            "inactive": counts.get(MCPToolStatus.INACTIVE.value, 0),
            "missing": missing,
            "blocked": blocked,
            "problematic": missing + blocked,
        }

    async def group_counts(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        column: Any,
    ) -> dict[str, int]:
        stmt = (
            select(column, func.count())
            .where(
                Execution.requested_at >= from_time,
                Execution.requested_at < to_time,
            )
            .group_by(column)
        )
        rows = (await self._session.execute(stmt)).all()
        out: dict[str, int] = {}
        for key, count in rows:
            if key is None:
                continue
            out[str(key)] = int(count)
        return out

    async def top_error_codes(
        self, *, from_time: datetime, to_time: datetime, limit: int = 10
    ) -> list[tuple[str, int]]:
        stmt = (
            select(Execution.error_code, func.count())
            .where(
                Execution.requested_at >= from_time,
                Execution.requested_at < to_time,
                Execution.error_code.is_not(None),
            )
            .group_by(Execution.error_code)
            .order_by(func.count().desc(), Execution.error_code.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(str(code), int(count)) for code, count in rows]

    async def error_codes_for_category(
        self, *, from_time: datetime, to_time: datetime
    ) -> list[str | None]:
        """Return error_code values in window for category aggregation in Python."""
        stmt = select(Execution.error_code).where(
            Execution.requested_at >= from_time,
            Execution.requested_at < to_time,
        )
        return [row[0] for row in (await self._session.execute(stmt)).all()]

    async def queue_health(self, *, now: datetime) -> dict[str, Any]:
        counts = (
            await self._session.execute(
                select(Execution.status, func.count()).where(
                    Execution.status.in_(
                        [
                            ExecutionStatus.CREATED.value,
                            ExecutionStatus.QUEUED.value,
                            ExecutionStatus.RUNNING.value,
                        ]
                    )
                ).group_by(Execution.status)
            )
        ).all()
        by = {str(s): int(c) for s, c in counts}

        oldest_created = (
            await self._session.execute(
                select(func.min(Execution.requested_at)).where(
                    Execution.status == ExecutionStatus.CREATED.value
                )
            )
        ).scalar_one()
        oldest_queued = (
            await self._session.execute(
                select(func.min(Execution.queued_at)).where(
                    Execution.status == ExecutionStatus.QUEUED.value,
                    Execution.queued_at.is_not(None),
                )
            )
        ).scalar_one()

        def _age(ts: datetime | None) -> float | None:
            if ts is None:
                return None
            if ts.tzinfo is None:
                return None
            age = (now - ts).total_seconds()
            return age if age >= 0 else 0.0

        return {
            "created_count": by.get(ExecutionStatus.CREATED.value, 0),
            "queued_count": by.get(ExecutionStatus.QUEUED.value, 0),
            "running_count": by.get(ExecutionStatus.RUNNING.value, 0),
            "oldest_created_age_seconds": _age(oldest_created),
            "oldest_queued_age_seconds": _age(oldest_queued),
        }

    async def outbox_health(self, *, now: datetime) -> dict[str, Any]:
        pending = (
            await self._session.execute(
                select(func.count()).where(OutboxEvent.published_at.is_(None))
            )
        ).scalar_one()
        oldest = (
            await self._session.execute(
                select(func.min(OutboxEvent.created_at)).where(
                    OutboxEvent.published_at.is_(None)
                )
            )
        ).scalar_one()
        age = None
        if oldest is not None and oldest.tzinfo is not None:
            age = max(0.0, (now - oldest).total_seconds())
        return {
            "pending_count": int(pending),
            "oldest_pending_age_seconds": age,
        }


def and_duration_window(from_time: datetime, to_time: datetime) -> tuple:
    return (
        Execution.requested_at >= from_time,
        Execution.requested_at < to_time,
        Execution.status.in_(tuple(_TERMINAL)),
        Execution.started_at.is_not(None),
        Execution.finished_at.is_not(None),
        Execution.finished_at >= Execution.started_at,
    )


def _f(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def aggregate_error_categories(codes: list[str | None]) -> dict[str, int]:
    out: dict[str, int] = {}
    for code in codes:
        if code is None:
            continue
        cat = classify_error_category(error_code=code) or "unknown"
        out[cat] = out.get(cat, 0) + 1
    return out
