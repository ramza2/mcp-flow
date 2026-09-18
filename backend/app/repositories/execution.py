"""Execution persistence repository — docs/05 §13."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import Execution, ExecutionStep, StepAttempt


class ExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_execution(
        self,
        *,
        source_type: str,
        trigger_type: str,
        requester_id: uuid.UUID,
        agent_request_id: uuid.UUID | None,
        agent_version_id: uuid.UUID | None,
        plan_validation_run_id: uuid.UUID | None,
        status: str,
        plan_schema_version: str,
        plan_snapshot: dict[str, Any],
        plan_hash: str,
        input_snapshot: dict[str, Any],
        policy_snapshot: dict[str, Any],
        trace_id: str | None,
        requested_at: datetime,
        lock_version: int = 1,
        workflow_version_id: uuid.UUID | None = None,
        schedule_occurrence_id: uuid.UUID | None = None,
        parent_execution_id: uuid.UUID | None = None,
    ) -> Execution:
        row = Execution(
            id=uuid.uuid4(),
            source_type=source_type,
            trigger_type=trigger_type,
            requester_id=requester_id,
            agent_request_id=agent_request_id,
            agent_version_id=agent_version_id,
            workflow_version_id=workflow_version_id,
            schedule_occurrence_id=schedule_occurrence_id,
            parent_execution_id=parent_execution_id,
            plan_validation_run_id=plan_validation_run_id,
            status=status,
            plan_schema_version=plan_schema_version,
            plan_snapshot=dict(plan_snapshot),
            plan_hash=plan_hash,
            input_snapshot=dict(input_snapshot),
            policy_snapshot=dict(policy_snapshot),
            # Leave nullable JSONB unset so PostgreSQL stores SQL NULL
            # (explicit None becomes JSON null and fails jsonb_typeof CHECKs).
            error_code=None,
            error_message=None,
            trace_id=trace_id,
            priority=None,
            requested_at=requested_at,
            queued_at=None,
            started_at=None,
            finished_at=None,
            cancel_requested_at=None,
            lock_version=lock_version,
            retention_until=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_step(
        self,
        *,
        execution_id: uuid.UUID,
        step_key: str,
        step_type: str,
        mcp_tool_version_id: uuid.UUID | None,
        parent_step_id: uuid.UUID | None,
        sequence_hint: int,
        status: str,
        step_snapshot: dict[str, Any],
        lock_version: int = 1,
    ) -> ExecutionStep:
        row = ExecutionStep(
            id=uuid.uuid4(),
            execution_id=execution_id,
            step_key=step_key,
            step_type=step_type,
            mcp_tool_version_id=mcp_tool_version_id,
            parent_step_id=parent_step_id,
            sequence_hint=sequence_hint,
            status=status,
            step_snapshot=dict(step_snapshot),
            # Omit resolved_input / result_inline — SQL NULL, not JSON null.
            result_blob_id=None,
            condition_result=None,
            iteration_no=None,
            attempt_count=0,
            ready_at=None,
            started_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
            lock_version=lock_version,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, execution_id: uuid.UUID) -> Execution | None:
        stmt = select(Execution).where(Execution.id == execution_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_steps(self, execution_id: uuid.UUID) -> list[ExecutionStep]:
        stmt = (
            select(ExecutionStep)
            .where(ExecutionStep.execution_id == execution_id)
            .order_by(ExecutionStep.sequence_hint.asc(), ExecutionStep.step_key.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_for_agent_request(self, agent_request_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Execution).where(
            Execution.agent_request_id == agent_request_id
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def get_step(self, step_id: uuid.UUID) -> ExecutionStep | None:
        stmt = select(ExecutionStep).where(ExecutionStep.id == step_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create_attempt(
        self,
        *,
        step_execution_id: uuid.UUID,
        attempt_no: int,
        status: str,
        worker_id: str | None,
        lease_expires_at: datetime | None,
        idempotency_key: str,
        request_snapshot: dict[str, Any] | None,
        started_at: datetime,
    ) -> StepAttempt:
        row = StepAttempt(
            id=uuid.uuid4(),
            step_execution_id=step_execution_id,
            attempt_no=attempt_no,
            status=status,
            worker_id=worker_id,
            lease_expires_at=lease_expires_at,
            idempotency_key=idempotency_key,
            request_snapshot=(
                dict(request_snapshot) if request_snapshot is not None else None
            ),
            result_blob_id=None,
            error_layer=None,
            error_code=None,
            error_message=None,
            is_retryable=None,
            started_at=started_at,
            finished_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_attempts(self, step_execution_id: uuid.UUID) -> list[StepAttempt]:
        stmt = (
            select(StepAttempt)
            .where(StepAttempt.step_execution_id == step_execution_id)
            .order_by(StepAttempt.attempt_no.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_attempt_by_no(
        self, *, step_execution_id: uuid.UUID, attempt_no: int
    ) -> StepAttempt | None:
        stmt = select(StepAttempt).where(
            StepAttempt.step_execution_id == step_execution_id,
            StepAttempt.attempt_no == attempt_no,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_started_attempt(
        self, step_execution_id: uuid.UUID
    ) -> StepAttempt | None:
        stmt = (
            select(StepAttempt)
            .where(
                StepAttempt.step_execution_id == step_execution_id,
                StepAttempt.status == "STARTED",
            )
            .order_by(StepAttempt.attempt_no.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
