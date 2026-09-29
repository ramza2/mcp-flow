"""Reject an OPEN MCPInputRequest — user decision, zero MCP calls."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    McpInputRequestStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    UserStatus,
)
from app.execution.mrtr_wait import assert_durable_waiting_input
from app.models.auth import User
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository

_MRTR_REJECTED = "MRTR_REJECTED"
_MRTR_REJECTED_MESSAGE = "User rejected the MCP runtime input request."


@dataclass(frozen=True, slots=True)
class MrtrRejectOutcome:
    input_request_id: uuid.UUID
    execution_id: uuid.UUID
    status: str
    execution_status: str
    step_status: str


class MrtrRejectService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._executions = ExecutionRepository(session)
        self._inputs = MCPInputRequestRepository(session)

    async def reject(
        self,
        *,
        execution_id: uuid.UUID,
        input_request_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        now: datetime | None = None,
    ) -> MrtrRejectOutcome:
        ts = now or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        await self._assert_actor_active(actor_user_id)

        execution = await self._executions.lock_execution(execution_id)
        if execution is None:
            raise AppError(
                code="NOT_FOUND", message="Execution not found.", status_code=404
            )
        steps = await self._executions.list_steps(execution.id)
        if len(steps) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentRequest Execution must contain exactly one Step.",
                status_code=409,
            )
        step = await self._executions.lock_step(steps[0].id)
        assert step is not None

        if execution.requester_id != actor_user_id:
            raise AppError(
                code="NOT_FOUND",
                message="MCPInputRequest not found.",
                status_code=404,
            )

        request = await self._inputs.get_with_lock(input_request_id)
        if request is None or request.execution_id != execution.id:
            raise AppError(
                code="NOT_FOUND",
                message="MCPInputRequest not found.",
                status_code=404,
            )

        if request.status == McpInputRequestStatus.REJECTED.value:
            # Duplicate reject — deterministic no-op when already terminalized.
            if (
                execution.status == ExecutionStatus.FAILED.value
                and execution.error_code == _MRTR_REJECTED
            ):
                return MrtrRejectOutcome(
                    input_request_id=request.id,
                    execution_id=execution.id,
                    status=request.status,
                    execution_status=execution.status,
                    step_status=step.status,
                )
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest reject evidence is inconsistent.",
                status_code=409,
            )

        if request.status != McpInputRequestStatus.OPEN.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest is no longer OPEN.",
                status_code=409,
            )

        open_req = await assert_durable_waiting_input(
            executions=self._executions,
            inputs=self._inputs,
            execution=execution,
            step=step,
        )
        if open_req.id != request.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest is not the current OPEN wait.",
                status_code=409,
            )

        request.status = McpInputRequestStatus.REJECTED.value
        request.answered_at = ts
        request.answered_by = actor_user_id
        request.response_payload = None

        attempt = await self._executions.get_attempt(request.step_attempt_id)
        if attempt is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest Attempt is missing.",
                status_code=409,
            )
        attempt.status = StepAttemptStatus.FAILED.value
        attempt.error_layer = "PROTOCOL"
        attempt.error_code = _MRTR_REJECTED
        attempt.error_message = _MRTR_REJECTED_MESSAGE
        attempt.is_retryable = False
        attempt.finished_at = ts
        attempt.worker_id = None
        attempt.lease_expires_at = None

        # Completed ToolCall rounds remain SUCCEEDED evidence; do not mutate them.
        tool_calls = await self._executions.list_tool_calls(attempt.id)
        for tc in tool_calls:
            if tc.normalized_status == ToolCallNormalizedStatus.STARTED.value:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Cannot reject while a ToolCall is STARTED.",
                    status_code=409,
                )

        step.status = StepStatus.FAILED.value
        step.error_code = _MRTR_REJECTED
        step.error_message = _MRTR_REJECTED_MESSAGE
        step.finished_at = ts
        step.lock_version += 1

        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = _MRTR_REJECTED
        execution.error_message = _MRTR_REJECTED_MESSAGE
        execution.finished_at = ts
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.lock_version += 1

        await self._session.commit()
        return MrtrRejectOutcome(
            input_request_id=request.id,
            execution_id=execution.id,
            status=request.status,
            execution_status=execution.status,
            step_status=step.status,
        )

    async def _assert_actor_active(self, user_id: uuid.UUID) -> None:
        stmt = select(User).where(User.id == user_id)
        user = (await self._session.execute(stmt)).scalar_one_or_none()
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=403,
            )
