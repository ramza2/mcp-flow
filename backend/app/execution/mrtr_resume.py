"""Same-Execution MRTR resume claim after ANSWERED MCPInputRequest (PR #41).

WAITING_INPUT + ANSWERED → RUNNING with a fresh lease. Attempt stays STARTED.
Does not create ToolCall here — Runner creates the next ToolCall round.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    McpInputRequestStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import _normalize_worker_id
from app.execution.lineage import assert_resume_attempt_lineage
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.execution.runtime_preflight import assert_source_tool_executable
from app.models.execution import Execution, ExecutionStep
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository


@dataclass(frozen=True, slots=True)
class MrtrResumeClaimOutcome:
    execution_id: uuid.UUID
    input_request_id: uuid.UUID
    claimed: bool
    status: str | None
    worker_id: str | None
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    reason: str | None = None


class MrtrResumeClaimService:
    def __init__(self, session: AsyncSession, *, lease_seconds: int) -> None:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        self._session = session
        self._lease_seconds = lease_seconds
        self._executions = ExecutionRepository(session)
        self._inputs = MCPInputRequestRepository(session)

    async def claim(
        self,
        *,
        execution_id: uuid.UUID,
        input_request_id: uuid.UUID,
        worker_id: str,
        now: datetime | None = None,
    ) -> MrtrResumeClaimOutcome:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            return MrtrResumeClaimOutcome(
                execution_id=execution_id,
                input_request_id=input_request_id,
                claimed=False,
                status=None,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="MISSING",
            )

        if execution.status != ExecutionStatus.WAITING_INPUT.value:
            return MrtrResumeClaimOutcome(
                execution_id=execution.id,
                input_request_id=input_request_id,
                claimed=False,
                status=execution.status,
                worker_id=execution.worker_id,
                lease_token=None,
                lease_expires_at=execution.lease_expires_at,
                reason="STALE_DELIVERY",
            )

        steps = await self._executions.list_steps(execution.id)
        waiting_steps = [
            s for s in steps if s.status == StepStatus.WAITING_INPUT.value
        ]
        if len(waiting_steps) != 1:
            # Single-TOOL AgentRequest and single waiting Workflow TOOL are
            # supported; multi-wait is fail-closed.
            if (
                execution.source_type == ExecutionSourceType.AGENT_REQUEST.value
                and len(steps) != 1
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="AgentRequest Execution must contain exactly one Step.",
                    status_code=409,
                )
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MRTR resume requires exactly one WAITING_INPUT Step.",
                status_code=409,
            )
        step = await self._executions.lock_step(waiting_steps[0].id)
        assert step is not None
        if step.status != StepStatus.WAITING_INPUT.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Step is not WAITING_INPUT for MRTR resume.",
                status_code=409,
            )

        if (
            execution.worker_id is not None
            or execution.lease_token is not None
            or execution.lease_expires_at is not None
            or execution.heartbeat_at is not None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WAITING_INPUT Execution must not retain worker lease fields.",
                status_code=409,
            )

        open_rows = await self._inputs.list_open_for_step(
            execution_id=execution.id, step_execution_id=step.id
        )
        if open_rows:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Cannot resume while an OPEN MCPInputRequest exists.",
                status_code=409,
            )

        request = await self._inputs.get_with_lock(input_request_id)
        if (
            request is None
            or request.execution_id != execution.id
            or request.step_execution_id != step.id
            or request.status != McpInputRequestStatus.ANSWERED.value
            or not isinstance(request.response_payload, dict)
            or not request.response_payload
        ):
            await self._terminalize_precondition_failed(execution, step, ts)
            await self._session.flush()
            return MrtrResumeClaimOutcome(
                execution_id=execution.id,
                input_request_id=input_request_id,
                claimed=False,
                status=execution.status,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="MRTR_RESUME_PRECONDITION_FAILED",
            )

        attempt = await self._executions.get_attempt_with_lock(request.step_attempt_id)
        if (
            attempt is None
            or attempt.status != StepAttemptStatus.STARTED.value
            or attempt.step_execution_id != step.id
            or attempt.finished_at is not None
            or attempt.worker_id is not None
            or attempt.lease_expires_at is not None
        ):
            await self._terminalize_precondition_failed(execution, step, ts)
            await self._session.flush()
            return MrtrResumeClaimOutcome(
                execution_id=execution.id,
                input_request_id=input_request_id,
                claimed=False,
                status=execution.status,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="MRTR_RESUME_PRECONDITION_FAILED",
            )

        try:
            assert_resume_attempt_lineage(
                execution=execution,
                step=step,
                attempt=attempt,
                steps=await self._executions.list_steps(execution.id),
                worker_id=None,
            )
            tool_calls = await self._executions.list_tool_calls(attempt.id)
            if any(
                tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
                for tc in tool_calls
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="MRTR resume must not retain a STARTED ToolCall.",
                    status_code=409,
                )
            succeeded = [
                tc
                for tc in tool_calls
                if tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
            ]
            if len(succeeded) != request.round_no or len(tool_calls) != request.round_no:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="MRTR resume ToolCall evidence does not match round_no.",
                    status_code=409,
                )
            expected_policy = get_expected_tool_policy_snapshot(
                execution,
                plan_step_id=str(step.step_snapshot.get("id") or step.step_key),
                tool_version_id=step.mcp_tool_version_id,
            )
            await assert_source_tool_executable(
                self._session,
                execution=execution,
                tool_version_id=step.mcp_tool_version_id,
                expected_policy_snapshot=expected_policy,
            )
        except AppError:
            await self._terminalize_precondition_failed(execution, step, ts)
            await self._session.flush()
            return MrtrResumeClaimOutcome(
                execution_id=execution.id,
                input_request_id=input_request_id,
                claimed=False,
                status=execution.status,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="MRTR_RESUME_PRECONDITION_FAILED",
            )

        lease_token = uuid.uuid4()
        lease_expires = ts + timedelta(seconds=self._lease_seconds)
        execution.status = ExecutionStatus.RUNNING.value
        execution.worker_id = worker
        execution.lease_token = lease_token
        execution.lease_expires_at = lease_expires
        execution.heartbeat_at = ts
        execution.error_code = None
        execution.error_message = None
        execution.lock_version += 1

        step.status = StepStatus.RUNNING.value
        step.error_code = None
        step.error_message = None
        step.lock_version += 1

        attempt.worker_id = worker
        attempt.lease_expires_at = lease_expires

        await self._session.flush()
        return MrtrResumeClaimOutcome(
            execution_id=execution.id,
            input_request_id=request.id,
            claimed=True,
            status=execution.status,
            worker_id=worker,
            lease_token=lease_token,
            lease_expires_at=lease_expires,
            reason=None,
        )

    async def _terminalize_precondition_failed(
        self, execution: Execution, step: ExecutionStep, ts: datetime
    ) -> None:
        if step.status not in {
            StepStatus.FAILED.value,
            StepStatus.SUCCEEDED.value,
            StepStatus.CANCELLED.value,
            StepStatus.TIMED_OUT.value,
            StepStatus.UNKNOWN_OUTCOME.value,
        }:
            step.status = StepStatus.FAILED.value
            step.error_code = "MRTR_RESUME_PRECONDITION_FAILED"
            step.error_message = "MRTR resume preflight failed."
            step.finished_at = ts
            step.lock_version += 1
        if execution.status == ExecutionStatus.WAITING_INPUT.value:
            execution.status = ExecutionStatus.FAILED.value
            execution.error_code = "MRTR_RESUME_PRECONDITION_FAILED"
            execution.error_message = "MRTR resume preflight failed."
            execution.finished_at = ts
            execution.worker_id = None
            execution.lease_token = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.lock_version += 1
