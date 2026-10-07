"""Answer an OPEN MCPInputRequest and enqueue same-Execution MRTR resume."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    McpInputRequestStatus,
    UserStatus,
)
from app.execution.claim import _as_utc
from app.execution.mrtr_response_validate import validate_mrtr_responses
from app.execution.mrtr_wait import assert_durable_waiting_input
from app.models.auth import User
from app.models.execution import Execution
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from app.repositories.outbox import OutboxRepository


@dataclass(frozen=True, slots=True)
class MrtrResponseOutcome:
    input_request_id: uuid.UUID
    execution_id: uuid.UUID
    status: str
    resume_enqueued: bool
    execution_status: str
    step_status: str


class MrtrResponseService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._executions = ExecutionRepository(session)
        self._inputs = MCPInputRequestRepository(session)
        self._outbox = OutboxRepository(session)

    async def submit_response(
        self,
        *,
        execution_id: uuid.UUID,
        input_request_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        responses: dict[str, Any],
        now: datetime | None = None,
    ) -> MrtrResponseOutcome:
        ts = now or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        await self._assert_actor_active(actor_user_id)

        execution = await self._lock_execution(execution_id)
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

        # Duplicate identical answer — safe no-op when already ANSWERED with same payload.
        if request.status == McpInputRequestStatus.ANSWERED.value:
            if request.response_payload == responses and request.answered_by == actor_user_id:
                return MrtrResponseOutcome(
                    input_request_id=request.id,
                    execution_id=execution.id,
                    status=request.status,
                    resume_enqueued=False,
                    execution_status=execution.status,
                    step_status=step.status,
                )
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest is no longer OPEN.",
                status_code=409,
            )

        if request.status != McpInputRequestStatus.OPEN.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest is no longer OPEN.",
                status_code=409,
            )

        # Full durable wait evidence (OPEN + lineage) before mutation.
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
        if request.step_execution_id != step.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest step lineage is inconsistent.",
                status_code=409,
            )

        if request.expires_at is not None and _as_utc(request.expires_at) <= _as_utc(ts):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="MCPInputRequest has expired.",
                status_code=409,
            )

        accepted = validate_mrtr_responses(
            input_requests=dict(request.input_requests),
            responses=responses,
        )

        request.status = McpInputRequestStatus.ANSWERED.value
        request.response_payload = accepted
        request.answered_at = ts
        request.answered_by = actor_user_id

        await self._outbox.create_execution_mrtr_resume(
            execution_id=execution.id,
            input_request_id=request.id,
            created_at=ts,
        )
        await self._session.commit()

        return MrtrResponseOutcome(
            input_request_id=request.id,
            execution_id=execution.id,
            status=request.status,
            resume_enqueued=True,
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

    async def _lock_execution(self, execution_id: uuid.UUID) -> Execution:
        execution = await self._executions.lock_execution(execution_id)
        if execution is None:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=404,
            )
        if execution.status != ExecutionStatus.WAITING_INPUT.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution is not WAITING_INPUT.",
                status_code=409,
            )
        return execution
