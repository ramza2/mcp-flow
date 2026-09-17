"""DB-backed Execution orchestration claim/lease foundation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
)
from app.models.execution import Execution, ExecutionStep
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    ExecutionPlanStep,
    ExecutionPlanV1,
    ToolStepConfigV1,
    compute_plan_hash,
)

_WORKER_ID_MAX_LEN = 128


def _as_utc(value: datetime) -> datetime:
    """Normalize DB datetimes for comparisons across PostgreSQL and SQLite tests."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ExecutionClaimOutcome:
    execution_id: uuid.UUID
    claimed: bool
    status: str | None
    worker_id: str | None
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    ready_step_ids: tuple[uuid.UUID, ...] = ()
    reason: str | None = None


def _normalize_worker_id(worker_id: str) -> str:
    value = worker_id.strip()
    if not value or len(value) > _WORKER_ID_MAX_LEN:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"worker_id must be between 1 and {_WORKER_ID_MAX_LEN} characters.",
            status_code=400,
        )
    return value


class ExecutionClaimService:
    def __init__(self, session: AsyncSession, *, lease_seconds: int) -> None:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        self._session = session
        self._lease_seconds = lease_seconds

    async def claim(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        now: datetime | None = None,
    ) -> ExecutionClaimOutcome:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)
        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            return ExecutionClaimOutcome(
                execution_id=execution_id,
                claimed=False,
                status=None,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="MISSING",
            )
        if execution.status != ExecutionStatus.QUEUED.value:
            return ExecutionClaimOutcome(
                execution_id=execution.id,
                claimed=False,
                status=execution.status,
                worker_id=execution.worker_id,
                lease_token=None,
                lease_expires_at=execution.lease_expires_at,
                reason="STALE_DELIVERY",
            )
        if execution.source_type != ExecutionSourceType.AGENT_REQUEST.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Queue/Claim foundation supports AgentRequest Executions only.",
                status_code=409,
            )
        if execution.queued_at is None or execution.started_at is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="QUEUED Execution lifecycle timestamps are inconsistent.",
                status_code=409,
            )
        if any(
            value is not None
            for value in (
                execution.worker_id,
                execution.lease_token,
                execution.lease_expires_at,
                execution.heartbeat_at,
            )
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="QUEUED Execution unexpectedly owns a lease.",
                status_code=409,
            )

        step_stmt = (
            select(ExecutionStep)
            .where(ExecutionStep.execution_id == execution.id)
            .order_by(ExecutionStep.sequence_hint.asc(), ExecutionStep.step_key.asc())
            .with_for_update()
        )
        steps = list((await self._session.execute(step_stmt)).scalars().all())
        if len(steps) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentRequest foundation Execution must contain exactly one Step.",
                status_code=409,
            )
        step = steps[0]
        if (
            step.step_key != DETERMINISTIC_TOOL_STEP_ID
            or step.step_type != AuthorableStepType.TOOL.value
            or step.status != StepStatus.PENDING.value
            or step.parent_step_id is not None
            or step.sequence_hint != 0
            or step.ready_at is not None
            or step.started_at is not None
            or step.attempt_count != 0
            or step.resolved_input is not None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ExecutionStep is inconsistent with initial PENDING foundation state.",
                status_code=409,
            )

        try:
            plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
            plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan/step snapshot is invalid.",
                status_code=409,
            ) from exc
        if (
            execution.plan_schema_version != plan.schema_version
            or compute_plan_hash(execution.plan_snapshot) != execution.plan_hash
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan snapshot/hash lineage is inconsistent.",
                status_code=409,
            )
        if len(plan.steps) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentRequest foundation Execution plan must contain one Step.",
                status_code=409,
            )
        expected_step = plan.steps[0]
        if expected_step.model_dump(mode="json") != step.step_snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan/step snapshot lineage is inconsistent.",
                status_code=409,
            )
        try:
            tool_config = ToolStepConfigV1.model_validate(expected_step.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution TOOL Step config is invalid.",
                status_code=409,
            ) from exc
        if (
            plan_step.id != step.step_key
            or plan_step.id != expected_step.id
            or plan_step.type != AuthorableStepType.TOOL
            or plan_step.depends_on != []
            or plan_step.when is not None
            or step.mcp_tool_version_id != tool_config.tool_version_id
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Initial TOOL Step is not readyable by the foundation claim path.",
                status_code=409,
            )

        token = uuid.uuid4()
        expires_at = ts + timedelta(seconds=self._lease_seconds)
        execution.status = ExecutionStatus.RUNNING.value
        execution.worker_id = worker
        execution.lease_token = token
        execution.lease_expires_at = expires_at
        execution.heartbeat_at = ts
        execution.started_at = ts
        execution.lock_version += 1

        step.status = StepStatus.READY.value
        step.ready_at = ts
        step.lock_version += 1
        await self._session.flush()

        return ExecutionClaimOutcome(
            execution_id=execution.id,
            claimed=True,
            status=execution.status,
            worker_id=worker,
            lease_token=token,
            lease_expires_at=expires_at,
            ready_step_ids=(step.id,),
        )

    async def renew_lease(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
        now: datetime | None = None,
    ) -> datetime:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)
        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution lease no longer exists.",
                status_code=409,
            )
        lease_expires_at = (
            _as_utc(execution.lease_expires_at)
            if execution.lease_expires_at is not None
            else None
        )
        if (
            execution.status != ExecutionStatus.RUNNING.value
            or execution.worker_id != worker
            or execution.lease_token != lease_token
            or lease_expires_at is None
            or lease_expires_at <= ts
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution lease cannot be renewed.",
                status_code=409,
            )
        expires_at = ts + timedelta(seconds=self._lease_seconds)
        execution.heartbeat_at = ts
        execution.lease_expires_at = expires_at
        execution.lock_version += 1
        await self._session.flush()
        return expires_at
