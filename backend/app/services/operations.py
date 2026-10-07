"""Operations Dashboard / stats / system-health service (FNC-OPS-001).

Requires ACTIVE User + execution.read. Aggregate-only for MCP/Approval/Schedule —
does not grant detailed resource permissions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    ExecutionTriggerType,
    UserStatus,
)
from app.ops.error_category import ERROR_CATEGORIES
from app.repositories.operations import (
    OperationsRepository,
    aggregate_error_category_counts,
)
from app.repositories.user import UserRepository
from app.schemas.operations import (
    ApprovalOpsSummary,
    DashboardSummaryResponse,
    DatabaseHealth,
    DurationStats,
    ErrorCodeCount,
    ExecutionQueueHealth,
    ExecutionStatsResponse,
    ExecutionStatusCounts,
    MCPServerOpsSummary,
    MCPToolOpsSummary,
    OutboxHealth,
    ScheduleOpsSummary,
    SchedulerHealth,
    SystemHealthResponse,
)
from app.services.authorization import AuthorizationResolver
from app.services.execution_query import ExecutionQueryService
from app.services.health import DatabasePing, check_readiness, ping_database

_EXECUTION_READ = "execution.read"
_MAX_RECENT = 20


class OperationsService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        database_ping: DatabasePing | None = None,
    ) -> None:
        self._session = session
        self._ops = OperationsRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._exec_query = ExecutionQueryService(session)
        self._settings = settings or get_settings()
        self._database_ping = database_ping

    async def _assert_operator(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        if not await self._authz.has_permission(actor_user_id, _EXECUTION_READ):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing execution.read permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    @staticmethod
    def _require_aware(value: datetime | None, *, field: str) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"{field} must be timezone-aware.",
                status_code=422,
            )
        return value

    def _window(
        self,
        *,
        from_time: datetime | None,
        to_time: datetime | None,
        now: datetime,
    ) -> tuple[datetime, datetime]:
        to_v = self._require_aware(to_time, field="to") or now
        from_v = self._require_aware(from_time, field="from") or (
            to_v - timedelta(hours=24)
        )
        if from_v >= to_v:
            raise AppError(
                code="VALIDATION_ERROR",
                message="from must be earlier than to.",
                status_code=422,
            )
        return from_v, to_v

    @staticmethod
    def _status_counts(raw: dict[str, int]) -> ExecutionStatusCounts:
        def g(status: ExecutionStatus) -> int:
            return raw.get(status.value, 0)

        total = sum(raw.values())
        return ExecutionStatusCounts(
            total=total,
            created=g(ExecutionStatus.CREATED),
            queued=g(ExecutionStatus.QUEUED),
            running=g(ExecutionStatus.RUNNING),
            waiting_input=g(ExecutionStatus.WAITING_INPUT),
            waiting_approval=g(ExecutionStatus.WAITING_APPROVAL),
            cancel_requested=g(ExecutionStatus.CANCEL_REQUESTED),
            succeeded=g(ExecutionStatus.SUCCEEDED),
            partially_succeeded=g(ExecutionStatus.PARTIALLY_SUCCEEDED),
            failed=g(ExecutionStatus.FAILED),
            cancelled=g(ExecutionStatus.CANCELLED),
            timed_out=g(ExecutionStatus.TIMED_OUT),
        )

    @staticmethod
    def _success_rate(counts: ExecutionStatusCounts) -> tuple[int, float | None]:
        terminal_total = (
            counts.succeeded
            + counts.partially_succeeded
            + counts.failed
            + counts.cancelled
            + counts.timed_out
        )
        if terminal_total == 0:
            return 0, None
        return terminal_total, counts.succeeded / terminal_total

    async def dashboard_summary(
        self,
        *,
        actor_user_id: uuid.UUID,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        recent_limit: int = 5,
        now: datetime | None = None,
    ) -> DashboardSummaryResponse:
        await self._assert_operator(actor_user_id)
        if recent_limit < 1 or recent_limit > _MAX_RECENT:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"recent_limit must be between 1 and {_MAX_RECENT}.",
                status_code=422,
            )
        ts = now or datetime.now(UTC)
        window_from, window_to = self._window(
            from_time=from_time, to_time=to_time, now=ts
        )

        raw_counts = await self._ops.execution_status_counts(
            from_time=window_from, to_time=window_to
        )
        counts = self._status_counts(raw_counts)
        terminal_total, success_rate = self._success_rate(counts)
        durations = await self._ops.duration_aggregates(
            from_time=window_from, to_time=window_to
        )

        pending, overdue = await self._ops.approval_counts(now=ts)
        schedules = await self._ops.schedule_counts(
            now=ts,
            misfire_grace_seconds=self._settings.scheduler_misfire_grace_seconds,
        )
        servers = await self._ops.mcp_server_counts()
        tools = await self._ops.mcp_tool_counts()

        # Recent list is global latest (requested_at DESC), independent of the
        # metrics from/to window — window scopes aggregates only.
        recent_rows = await self._ops.recent_executions(
            limit=recent_limit, from_time=None, to_time=None
        )
        recent = await self._exec_query.project_list_items(recent_rows, now=ts)

        return DashboardSummaryResponse(
            window_from=window_from,
            window_to=window_to,
            generated_at=ts,
            executions=counts,
            terminal_total=terminal_total,
            success_rate=success_rate,
            avg_duration_ms=durations["avg_ms"],
            p95_duration_ms=durations["p95_ms"],
            approvals=ApprovalOpsSummary(pending=pending, overdue=overdue),
            schedules=ScheduleOpsSummary(**schedules),
            mcp_servers=MCPServerOpsSummary(**servers),
            mcp_tools=MCPToolOpsSummary(**tools),
            recent_executions=recent,
        )

    async def execution_stats(
        self,
        *,
        actor_user_id: uuid.UUID,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        now: datetime | None = None,
    ) -> ExecutionStatsResponse:
        from app.models.execution import Execution

        await self._assert_operator(actor_user_id)
        ts = now or datetime.now(UTC)
        window_from, window_to = self._window(
            from_time=from_time, to_time=to_time, now=ts
        )

        by_status = await self._ops.group_counts(
            from_time=window_from, to_time=window_to, column=Execution.status
        )
        by_source = await self._ops.group_counts(
            from_time=window_from, to_time=window_to, column=Execution.source_type
        )
        by_trigger = await self._ops.group_counts(
            from_time=window_from, to_time=window_to, column=Execution.trigger_type
        )
        # Ensure canonical keys appear (0 when missing) for status/source/trigger.
        for st in ExecutionStatus:
            by_status.setdefault(st.value, 0)
        for st in ExecutionSourceType:
            by_source.setdefault(st.value, 0)
        for st in ExecutionTriggerType:
            by_trigger.setdefault(st.value, 0)

        code_counts = await self._ops.error_code_counts(
            from_time=window_from, to_time=window_to
        )
        by_category = aggregate_error_category_counts(code_counts)
        for cat in ERROR_CATEGORIES:
            by_category.setdefault(cat, 0)

        top = await self._ops.top_error_codes(
            from_time=window_from, to_time=window_to, limit=10
        )
        durations = await self._ops.duration_aggregates(
            from_time=window_from, to_time=window_to
        )

        return ExecutionStatsResponse(
            window_from=window_from,
            window_to=window_to,
            by_status=by_status,
            by_source_type=by_source,
            by_trigger_type=by_trigger,
            by_error_category=by_category,
            top_error_codes=[
                ErrorCodeCount(error_code=code, count=count) for code, count in top
            ],
            duration=DurationStats(
                avg_ms=durations["avg_ms"],
                p50_ms=durations["p50_ms"],
                p95_ms=durations["p95_ms"],
                max_ms=durations["max_ms"],
            ),
        )

    async def system_health(
        self,
        *,
        actor_user_id: uuid.UUID,
        now: datetime | None = None,
    ) -> SystemHealthResponse:
        await self._assert_operator(actor_user_id)
        ts = now or datetime.now(UTC)
        checks = await check_readiness(database_ping=self._database_ping or ping_database)
        db_status = checks.get("database", "unavailable")

        queue = await self._ops.queue_health(now=ts)
        schedules = await self._ops.schedule_counts(
            now=ts,
            misfire_grace_seconds=self._settings.scheduler_misfire_grace_seconds,
        )
        outbox = await self._ops.outbox_health(now=ts)

        return SystemHealthResponse(
            generated_at=ts,
            database=DatabaseHealth(status=db_status),
            execution_queue=ExecutionQueueHealth(**queue),
            scheduler=SchedulerHealth(
                overdue_schedule_count=schedules["overdue"],
                error_schedule_count=schedules["error"],
            ),
            outbox=OutboxHealth(**outbox),
        )
