"""Execution history read service — own vs global execution.read (REQ-AUTH-006)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    ExecutionTriggerType,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    UserStatus,
)
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.ops.error_category import classify_error_category
from app.repositories.execution_query import (
    ExecutionListFilters,
    ExecutionQueryRepository,
)
from app.repositories.user import UserRepository
from app.schemas.execution_query import (
    ExecutionDetail,
    ExecutionListItem,
    ExecutionListResponse,
    ExecutionSourceProjection,
    ExecutionStepDetail,
    ExecutionStepListItem,
    ExecutionStepListResponse,
    PlanLimitsSummary,
    StepAttemptSafeItem,
    ToolCallSafeItem,
)
from app.services.authorization import AuthorizationResolver

_EXECUTION_READ = "execution.read"
_MAX_PAGE_SIZE = 100
_MAX_Q = 128
_ALLOWED_SORT = frozenset(
    {
        "requested_at",
        "-requested_at",
        "started_at",
        "-started_at",
        "finished_at",
        "-finished_at",
        "status",
        "-status",
    }
)

# Actively progressing statuses may use wall-clock `now` when finished_at is null.
# Terminal rows with missing finished_at must not fabricate an increasing duration.
_EXECUTION_ACTIVE_FOR_DURATION = frozenset(
    {
        ExecutionStatus.RUNNING.value,
        ExecutionStatus.WAITING_INPUT.value,
        ExecutionStatus.WAITING_APPROVAL.value,
        ExecutionStatus.CANCEL_REQUESTED.value,
    }
)
_STEP_ACTIVE_FOR_DURATION = frozenset(
    {
        StepStatus.READY.value,
        StepStatus.RUNNING.value,
        StepStatus.WAITING_INPUT.value,
        StepStatus.WAITING_APPROVAL.value,
    }
)
_ATTEMPT_ACTIVE_FOR_DURATION = frozenset({StepAttemptStatus.STARTED.value})
_TOOL_CALL_ACTIVE_FOR_DURATION = frozenset({ToolCallNormalizedStatus.STARTED.value})


@dataclass(frozen=True, slots=True)
class ExecutionListQuery:
    page: int = 1
    page_size: int = 20
    status: str | None = None
    source_type: str | None = None
    trigger_type: str | None = None
    requester_id: uuid.UUID | None = None
    agent_version_id: uuid.UUID | None = None
    workflow_version_id: uuid.UUID | None = None
    schedule_occurrence_id: uuid.UUID | None = None
    parent_execution_id: uuid.UUID | None = None
    tool_version_id: uuid.UUID | None = None
    error_code: str | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    q: str | None = None
    sort: str = "-requested_at"


class ExecutionQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ExecutionQueryRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)

    async def _assert_active(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    async def _visibility(
        self, actor_user_id: uuid.UUID
    ) -> tuple[bool, uuid.UUID | None]:
        """Return (can_read_all, scope_requester_id)."""
        await self._assert_active(actor_user_id)
        can_read_all = await self._authz.has_permission(actor_user_id, _EXECUTION_READ)
        if can_read_all:
            return True, None
        return False, actor_user_id

    async def _assert_can_view(
        self, *, actor_user_id: uuid.UUID, execution: Execution | None
    ) -> Execution:
        can_read_all, _scope = await self._visibility(actor_user_id)
        if execution is None:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if not can_read_all and execution.requester_id != actor_user_id:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return execution

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

    @staticmethod
    def _parse_csv_enum(raw: str | None, enum_cls: type, *, field: str) -> tuple[str, ...]:
        if raw is None or raw.strip() == "":
            return ()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            return ()
        out: list[str] = []
        for part in parts:
            try:
                out.append(enum_cls(part).value)
            except ValueError as exc:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"Invalid {field}: {part}.",
                    status_code=422,
                ) from exc
        return tuple(out)

    def _normalize_filters(
        self,
        query: ExecutionListQuery,
        *,
        can_read_all: bool,
        actor_user_id: uuid.UUID,
    ) -> ExecutionListFilters:
        if query.page < 1:
            raise AppError(
                code="VALIDATION_ERROR",
                message="page must be >= 1.",
                status_code=422,
            )
        if query.page_size < 1 or query.page_size > _MAX_PAGE_SIZE:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"page_size must be between 1 and {_MAX_PAGE_SIZE}.",
                status_code=422,
            )
        sort = query.sort or "-requested_at"
        if sort not in _ALLOWED_SORT:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"Invalid sort: {sort}.",
                status_code=422,
            )
        from_time = self._require_aware(query.from_time, field="from")
        to_time = self._require_aware(query.to_time, field="to")
        if from_time is not None and to_time is not None and from_time >= to_time:
            raise AppError(
                code="VALIDATION_ERROR",
                message="from must be earlier than to.",
                status_code=422,
            )
        q = query.q.strip() if isinstance(query.q, str) else None
        if q == "":
            q = None
        if q is not None and len(q) > _MAX_Q:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"q must be at most {_MAX_Q} characters.",
                status_code=422,
            )

        statuses = self._parse_csv_enum(
            query.status, ExecutionStatus, field="status"
        )
        source_types = self._parse_csv_enum(
            query.source_type, ExecutionSourceType, field="source_type"
        )
        trigger_types = self._parse_csv_enum(
            query.trigger_type, ExecutionTriggerType, field="trigger_type"
        )

        requester_id = query.requester_id
        if not can_read_all:
            if requester_id is not None and requester_id != actor_user_id:
                raise AppError(
                    code="AUTH_FORBIDDEN",
                    message="Cannot filter requester_id outside own history.",
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            # Own scope is always injected via scope_requester_id.
            requester_id = None

        return ExecutionListFilters(
            statuses=statuses,
            source_types=source_types,
            trigger_types=trigger_types,
            requester_id=requester_id,
            agent_version_id=query.agent_version_id,
            workflow_version_id=query.workflow_version_id,
            schedule_occurrence_id=query.schedule_occurrence_id,
            parent_execution_id=query.parent_execution_id,
            tool_version_id=query.tool_version_id,
            error_code=query.error_code,
            from_time=from_time,
            to_time=to_time,
            q=q,
            sort=sort,
            page=query.page,
            page_size=query.page_size,
            scope_requester_id=None if can_read_all else actor_user_id,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        # SQLite may round-trip timezone-aware columns as naive UTC.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @classmethod
    def duration_ms(
        cls,
        started_at: datetime | None,
        finished_at: datetime | None,
        *,
        now: datetime,
        status: str | None = None,
        active_statuses: frozenset[str] | None = None,
    ) -> int | None:
        """Elapsed ms from started_at → finished_at (or now when actively progressing).

        Terminal / non-active rows with missing ``finished_at`` return null so
        corrupt evidence does not fabricate a continuously increasing duration.
        Never returns a negative value.
        """
        if started_at is None:
            return None
        if finished_at is not None:
            end: datetime | None = finished_at
        elif (
            status is not None
            and active_statuses is not None
            and status in active_statuses
        ):
            end = now
        else:
            return None

        start = cls._aware(started_at)
        end_a = cls._aware(end)
        ms = int((end_a - start).total_seconds() * 1000)
        if ms < 0:
            return None
        return ms

    @classmethod
    def time_to_first_byte_ms(
        cls,
        started_at: datetime | None,
        first_byte_at: datetime | None,
    ) -> int | None:
        if started_at is None or first_byte_at is None:
            return None
        start = cls._aware(started_at)
        first = cls._aware(first_byte_at)
        if first < start:
            return None
        ms = int((first - start).total_seconds() * 1000)
        if ms < 0:
            return None
        return ms

    def _to_list_item(
        self,
        row: Execution,
        *,
        step_count: int,
        completed_step_count: int,
        failed_step_count: int,
        source: ExecutionSourceProjection | None,
        now: datetime,
    ) -> ExecutionListItem:
        return ExecutionListItem(
            id=row.id,
            source_type=ExecutionSourceType(row.source_type),
            trigger_type=ExecutionTriggerType(row.trigger_type),
            requester_id=row.requester_id,
            agent_request_id=row.agent_request_id,
            agent_version_id=row.agent_version_id,
            workflow_version_id=row.workflow_version_id,
            schedule_occurrence_id=row.schedule_occurrence_id,
            parent_execution_id=row.parent_execution_id,
            status=ExecutionStatus(row.status),
            error_code=row.error_code,
            error_category=classify_error_category(error_code=row.error_code),
            trace_id=row.trace_id,
            requested_at=row.requested_at,
            queued_at=row.queued_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            cancel_requested_at=row.cancel_requested_at,
            step_count=step_count,
            completed_step_count=completed_step_count,
            failed_step_count=failed_step_count,
            duration_ms=self.duration_ms(
                row.started_at,
                row.finished_at,
                now=now,
                status=row.status,
                active_statuses=_EXECUTION_ACTIVE_FOR_DURATION,
            ),
            source=source,
        )

    async def _project_items(
        self, rows: list[Execution], *, now: datetime
    ) -> list[ExecutionListItem]:
        ids = [r.id for r in rows]
        counts = await self._repo.step_counts_for(ids)
        sources = await self._repo.sources_for(rows)
        items: list[ExecutionListItem] = []
        for row in rows:
            sc = counts[row.id]
            src = sources[row.id]
            items.append(
                self._to_list_item(
                    row,
                    step_count=sc.step_count,
                    completed_step_count=sc.completed_step_count,
                    failed_step_count=sc.failed_step_count,
                    source=ExecutionSourceProjection(
                        type=ExecutionSourceType(src.source_type),
                        version_id=src.version_id,
                        logical_id=src.logical_id,
                        name=src.name,
                    ),
                    now=now,
                )
            )
        return items

    async def list_executions(
        self,
        *,
        actor_user_id: uuid.UUID,
        query: ExecutionListQuery,
        now: datetime | None = None,
    ) -> ExecutionListResponse:
        can_read_all, _ = await self._visibility(actor_user_id)
        filters = self._normalize_filters(
            query, can_read_all=can_read_all, actor_user_id=actor_user_id
        )
        total = await self._repo.count(filters)
        rows = await self._repo.list_page(filters)
        ts = now or datetime.now(UTC)
        items = await self._project_items(rows, now=ts)
        return ExecutionListResponse(
            items=items,
            page=filters.page,
            page_size=filters.page_size,
            total=total,
        )

    @staticmethod
    def _plan_limits(plan_snapshot: dict[str, Any]) -> PlanLimitsSummary | None:
        limits = plan_snapshot.get("limits")
        if not isinstance(limits, dict):
            return None
        try:
            return PlanLimitsSummary(
                max_steps=limits.get("max_steps"),
                max_duration_seconds=limits.get("max_duration_seconds"),
                max_parallelism=limits.get("max_parallelism"),
                max_loop_iterations=limits.get("max_loop_iterations"),
            )
        except Exception:  # noqa: BLE001
            return None

    async def get_execution(
        self,
        *,
        actor_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        now: datetime | None = None,
    ) -> ExecutionDetail:
        row = await self._repo.get(execution_id)
        await self._assert_can_view(actor_user_id=actor_user_id, execution=row)
        assert row is not None
        ts = now or datetime.now(UTC)
        items = await self._project_items([row], now=ts)
        base = items[0]
        # Operations boundary: expose only the existing minimal Execution.result_summary
        # (status / step_keys / step_count / step_statuses / response_steps status
        # metadata from build_result_summary). Do NOT project result_inline, Tool
        # content, structured_content, Tool metadata, input_snapshot, policy_snapshot,
        # or plan_snapshot. If runtime result_summary semantics change, re-review
        # this Operations response rather than auto-exposing arbitrary JSON.
        return ExecutionDetail(
            **base.model_dump(),
            plan_schema_version=row.plan_schema_version,
            plan_hash=row.plan_hash,
            plan_limits=self._plan_limits(row.plan_snapshot),
            result_summary=row.result_summary,
            retention_until=row.retention_until,
        )

    def _to_step_item(
        self, step: ExecutionStep, *, now: datetime
    ) -> ExecutionStepListItem:
        return ExecutionStepListItem(
            id=step.id,
            execution_id=step.execution_id,
            step_key=step.step_key,
            step_type=step.step_type,
            parent_step_id=step.parent_step_id,
            sequence_hint=step.sequence_hint,
            mcp_tool_version_id=step.mcp_tool_version_id,
            iteration_no=step.iteration_no,
            status=step.status,
            attempt_count=step.attempt_count,
            condition_result=step.condition_result,
            ready_at=step.ready_at,
            started_at=step.started_at,
            finished_at=step.finished_at,
            error_code=step.error_code,
            error_category=classify_error_category(error_code=step.error_code),
            duration_ms=self.duration_ms(
                step.started_at,
                step.finished_at,
                now=now,
                status=step.status,
                active_statuses=_STEP_ACTIVE_FOR_DURATION,
            ),
        )

    def _to_tool_call(self, tc: ToolCall, *, now: datetime) -> ToolCallSafeItem:
        return ToolCallSafeItem(
            id=tc.id,
            mcp_server_id=tc.mcp_server_id,
            mcp_tool_version_id=tc.mcp_tool_version_id,
            protocol_era=tc.protocol_era,
            protocol_version=tc.protocol_version,
            transport_type=tc.transport_type,
            normalized_status=tc.normalized_status,
            request_bytes=tc.request_bytes,
            response_bytes=tc.response_bytes,
            started_at=tc.started_at,
            first_byte_at=tc.first_byte_at,
            finished_at=tc.finished_at,
            duration_ms=self.duration_ms(
                tc.started_at,
                tc.finished_at,
                now=now,
                status=tc.normalized_status,
                active_statuses=_TOOL_CALL_ACTIVE_FOR_DURATION,
            ),
            time_to_first_byte_ms=self.time_to_first_byte_ms(
                tc.started_at, tc.first_byte_at
            ),
        )

    def _to_attempt(
        self,
        attempt: StepAttempt,
        tool_calls: list[ToolCall],
        *,
        now: datetime,
    ) -> StepAttemptSafeItem:
        return StepAttemptSafeItem(
            id=attempt.id,
            attempt_no=attempt.attempt_no,
            status=attempt.status,
            error_layer=attempt.error_layer,
            error_code=attempt.error_code,
            error_category=classify_error_category(
                error_code=attempt.error_code,
                error_layer=attempt.error_layer,
            ),
            is_retryable=attempt.is_retryable,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            duration_ms=self.duration_ms(
                attempt.started_at,
                attempt.finished_at,
                now=now,
                status=attempt.status,
                active_statuses=_ATTEMPT_ACTIVE_FOR_DURATION,
            ),
            tool_calls=[self._to_tool_call(tc, now=now) for tc in tool_calls],
        )

    async def list_steps(
        self,
        *,
        actor_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        now: datetime | None = None,
    ) -> ExecutionStepListResponse:
        execution = await self._repo.get(execution_id)
        await self._assert_can_view(actor_user_id=actor_user_id, execution=execution)
        ts = now or datetime.now(UTC)
        steps = await self._repo.list_steps(execution_id)
        return ExecutionStepListResponse(
            items=[self._to_step_item(s, now=ts) for s in steps]
        )

    async def get_step(
        self,
        *,
        actor_user_id: uuid.UUID,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        now: datetime | None = None,
    ) -> ExecutionStepDetail:
        execution = await self._repo.get(execution_id)
        await self._assert_can_view(actor_user_id=actor_user_id, execution=execution)
        step = await self._repo.get_step(
            execution_id=execution_id, step_execution_id=step_execution_id
        )
        if step is None:
            raise AppError(
                code="NOT_FOUND",
                message="Execution Step not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        ts = now or datetime.now(UTC)
        attempts_map = await self._repo.list_attempts([step.id])
        attempts = attempts_map.get(step.id, [])
        tool_map = await self._repo.list_tool_calls([a.id for a in attempts])
        base = self._to_step_item(step, now=ts)
        return ExecutionStepDetail(
            **base.model_dump(),
            attempts=[
                self._to_attempt(a, tool_map.get(a.id, []), now=ts) for a in attempts
            ],
        )

    async def project_list_items(
        self, rows: list[Execution], *, now: datetime
    ) -> list[ExecutionListItem]:
        """Shared projection for Dashboard recent list."""
        return await self._project_items(rows, now=now)
