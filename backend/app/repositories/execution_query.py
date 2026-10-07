"""Read-only Execution query repository — list/detail/steps (no FOR UPDATE)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import String, case, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.domain.enums import StepStatus
from app.models.agent import Agent, AgentVersion
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.models.workflow import Workflow, WorkflowVersion

_COMPLETED_STEP = frozenset(
    {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}
)
_FAILED_STEP = frozenset(
    {
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.UNKNOWN_OUTCOME.value,
    }
)

_SORT_COLUMNS = {
    "requested_at": Execution.requested_at,
    "started_at": Execution.started_at,
    "finished_at": Execution.finished_at,
    "status": Execution.status,
}
_NULLABLE_SORT = frozenset({"started_at", "finished_at"})


@dataclass(frozen=True, slots=True)
class ExecutionListFilters:
    statuses: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    trigger_types: tuple[str, ...] = ()
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
    page: int = 1
    page_size: int = 20
    # When set, restricts to this requester (own-history scope).
    scope_requester_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class StepCountRow:
    execution_id: uuid.UUID
    step_count: int
    completed_step_count: int
    failed_step_count: int


@dataclass(frozen=True, slots=True)
class SourceRow:
    execution_id: uuid.UUID
    source_type: str
    version_id: uuid.UUID | None
    logical_id: uuid.UUID | None
    name: str | None


class ExecutionQueryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_filtered(self, filters: ExecutionListFilters) -> Select[Any]:
        stmt: Select[Any] = select(Execution)
        if filters.scope_requester_id is not None:
            stmt = stmt.where(Execution.requester_id == filters.scope_requester_id)
        if filters.statuses:
            stmt = stmt.where(Execution.status.in_(filters.statuses))
        if filters.source_types:
            stmt = stmt.where(Execution.source_type.in_(filters.source_types))
        if filters.trigger_types:
            stmt = stmt.where(Execution.trigger_type.in_(filters.trigger_types))
        if filters.requester_id is not None:
            stmt = stmt.where(Execution.requester_id == filters.requester_id)
        if filters.agent_version_id is not None:
            stmt = stmt.where(Execution.agent_version_id == filters.agent_version_id)
        if filters.workflow_version_id is not None:
            stmt = stmt.where(
                Execution.workflow_version_id == filters.workflow_version_id
            )
        if filters.schedule_occurrence_id is not None:
            stmt = stmt.where(
                Execution.schedule_occurrence_id == filters.schedule_occurrence_id
            )
        if filters.parent_execution_id is not None:
            stmt = stmt.where(
                Execution.parent_execution_id == filters.parent_execution_id
            )
        if filters.error_code is not None:
            stmt = stmt.where(Execution.error_code == filters.error_code)
        if filters.from_time is not None:
            stmt = stmt.where(Execution.requested_at >= filters.from_time)
        if filters.to_time is not None:
            stmt = stmt.where(Execution.requested_at < filters.to_time)
        if filters.tool_version_id is not None:
            tool_exists = exists(
                select(ExecutionStep.id).where(
                    ExecutionStep.execution_id == Execution.id,
                    ExecutionStep.mcp_tool_version_id == filters.tool_version_id,
                )
            )
            stmt = stmt.where(tool_exists)
        if filters.q:
            # Literal substring — escape LIKE wildcards so q=% is not "match all".
            q = filters.q.strip()
            escaped = (
                q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            like = f"%{escaped}%"
            id_text = cast(Execution.id, String)
            stmt = stmt.where(
                or_(
                    id_text.ilike(like, escape="\\"),
                    Execution.trace_id.ilike(like, escape="\\"),
                    Execution.error_code.ilike(like, escape="\\"),
                )
            )
        return stmt

    def _apply_sort(self, stmt: Select[Any], sort: str) -> Select[Any]:
        desc = sort.startswith("-")
        key = sort[1:] if desc else sort
        col = _SORT_COLUMNS[key]
        primary = col.desc() if desc else col.asc()
        # Nullable started_at/finished_at: NULLS LAST for ASC and DESC so
        # unfinished rows do not float ahead of real timestamps under DESC.
        if key in _NULLABLE_SORT:
            primary = primary.nulls_last()
        # Deterministic tie-break on id DESC (stable UUID ordering).
        return stmt.order_by(primary, Execution.id.desc())

    async def count(self, filters: ExecutionListFilters) -> int:
        sub = self._base_filtered(filters).with_only_columns(Execution.id).subquery()
        result = await self._session.execute(select(func.count()).select_from(sub))
        return int(result.scalar_one())

    async def list_page(
        self, filters: ExecutionListFilters
    ) -> list[Execution]:
        stmt = self._apply_sort(self._base_filtered(filters), filters.sort)
        offset = (filters.page - 1) * filters.page_size
        stmt = stmt.offset(offset).limit(filters.page_size)
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def get(self, execution_id: uuid.UUID) -> Execution | None:
        return await self._session.get(Execution, execution_id)

    async def step_counts_for(
        self, execution_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, StepCountRow]:
        if not execution_ids:
            return {}
        completed = case(
            (ExecutionStep.status.in_(tuple(_COMPLETED_STEP)), 1),
            else_=0,
        )
        failed = case(
            (ExecutionStep.status.in_(tuple(_FAILED_STEP)), 1),
            else_=0,
        )
        stmt = (
            select(
                ExecutionStep.execution_id,
                func.count().label("step_count"),
                func.coalesce(func.sum(completed), 0).label("completed_step_count"),
                func.coalesce(func.sum(failed), 0).label("failed_step_count"),
            )
            .where(ExecutionStep.execution_id.in_(list(execution_ids)))
            .group_by(ExecutionStep.execution_id)
        )
        rows = (await self._session.execute(stmt)).all()
        out: dict[uuid.UUID, StepCountRow] = {}
        for r in rows:
            out[r.execution_id] = StepCountRow(
                execution_id=r.execution_id,
                step_count=int(r.step_count),
                completed_step_count=int(r.completed_step_count),
                failed_step_count=int(r.failed_step_count),
            )
        for eid in execution_ids:
            if eid not in out:
                out[eid] = StepCountRow(
                    execution_id=eid,
                    step_count=0,
                    completed_step_count=0,
                    failed_step_count=0,
                )
        return out

    async def sources_for(
        self, executions: Sequence[Execution]
    ) -> dict[uuid.UUID, SourceRow]:
        """Bulk source projection — no N+1, no Plan JSON parsing."""
        if not executions:
            return {}

        agent_version_ids = {
            e.agent_version_id
            for e in executions
            if e.agent_version_id is not None
            and e.source_type in {"AGENT_REQUEST", "SCHEDULE_OCCURRENCE"}
        }
        # Prefer agent_version for AGENT_REQUEST; workflow for WORKFLOW/SCHEDULE.
        wf_version_ids = {
            e.workflow_version_id
            for e in executions
            if e.workflow_version_id is not None
        }

        agent_map: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
        if agent_version_ids:
            rows = (
                await self._session.execute(
                    select(
                        AgentVersion.id,
                        Agent.id,
                        Agent.name,
                    )
                    .join(Agent, Agent.id == AgentVersion.agent_id)
                    .where(AgentVersion.id.in_(list(agent_version_ids)))
                )
            ).all()
            for av_id, agent_id, name in rows:
                agent_map[av_id] = (agent_id, name)

        wf_map: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
        if wf_version_ids:
            rows = (
                await self._session.execute(
                    select(
                        WorkflowVersion.id,
                        Workflow.id,
                        Workflow.name,
                    )
                    .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
                    .where(WorkflowVersion.id.in_(list(wf_version_ids)))
                )
            ).all()
            for wv_id, wf_id, name in rows:
                wf_map[wv_id] = (wf_id, name)

        out: dict[uuid.UUID, SourceRow] = {}
        for e in executions:
            version_id: uuid.UUID | None = None
            logical_id: uuid.UUID | None = None
            name: str | None = None
            if e.source_type == "AGENT_REQUEST" and e.agent_version_id is not None:
                version_id = e.agent_version_id
                info = agent_map.get(e.agent_version_id)
                if info:
                    logical_id, name = info
            elif e.source_type in {"WORKFLOW_VERSION", "SCHEDULE_OCCURRENCE"}:
                if e.workflow_version_id is not None:
                    version_id = e.workflow_version_id
                    info = wf_map.get(e.workflow_version_id)
                    if info:
                        logical_id, name = info
            else:
                # MANUAL_TOOL_TEST / FACTORY_TEST / unsupported
                version_id = e.agent_version_id or e.workflow_version_id
            out[e.id] = SourceRow(
                execution_id=e.id,
                source_type=e.source_type,
                version_id=version_id,
                logical_id=logical_id,
                name=name,
            )
        return out

    async def list_steps(self, execution_id: uuid.UUID) -> list[ExecutionStep]:
        stmt = (
            select(ExecutionStep)
            .where(ExecutionStep.execution_id == execution_id)
            .order_by(
                ExecutionStep.sequence_hint.asc(),
                ExecutionStep.step_key.asc(),
                ExecutionStep.id.asc(),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_step(
        self, *, execution_id: uuid.UUID, step_execution_id: uuid.UUID
    ) -> ExecutionStep | None:
        step = await self._session.get(ExecutionStep, step_execution_id)
        if step is None or step.execution_id != execution_id:
            return None
        return step

    async def list_attempts(
        self, step_execution_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[StepAttempt]]:
        if not step_execution_ids:
            return {}
        stmt = (
            select(StepAttempt)
            .where(StepAttempt.step_execution_id.in_(list(step_execution_ids)))
            .order_by(StepAttempt.attempt_no.asc(), StepAttempt.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        out: dict[uuid.UUID, list[StepAttempt]] = {
            sid: [] for sid in step_execution_ids
        }
        for row in rows:
            out.setdefault(row.step_execution_id, []).append(row)
        return out

    async def list_tool_calls(
        self, attempt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[ToolCall]]:
        if not attempt_ids:
            return {}
        stmt = (
            select(ToolCall)
            .where(ToolCall.step_attempt_id.in_(list(attempt_ids)))
            .order_by(ToolCall.started_at.asc(), ToolCall.id.asc())
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        out: dict[uuid.UUID, list[ToolCall]] = {aid: [] for aid in attempt_ids}
        for row in rows:
            out.setdefault(row.step_attempt_id, []).append(row)
        return out
