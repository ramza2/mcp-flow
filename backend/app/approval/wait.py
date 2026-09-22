"""ToolPolicy approval-wait entry (FNC-EXE-009 / FNC-APR-002 foundation).

Creates PENDING ApprovalRequest and transitions Execution/Step to
WAITING_APPROVAL with worker lease cleared — atomically in the caller TX.

Does not create StepAttempt / ToolCall or invoke MCP. Decision / resume are
out of scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.context import (
    build_approval_context_snapshot,
    compute_approval_context_hash,
)
from app.core.errors import AppError
from app.domain.enums import ExecutionStatus, StepStatus
from app.execution.lineage import materialize_secret_safe_resolved_input
from app.models.approval import ApprovalPolicy
from app.models.execution import Execution, ExecutionStep
from app.models.mcp import MCPToolPolicy
from app.repositories.approval_request import ApprovalRequestRepository
from app.schemas.execution_plan import ToolStepConfigV1


@dataclass(frozen=True, slots=True)
class ApprovalWaitOutcome:
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    approval_request_id: uuid.UUID
    context_hash: str
    reused_existing: bool


class ApprovalWaitService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = ApprovalRequestRepository(session)

    async def enter_for_tool_step(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        tool_config: ToolStepConfigV1,
        tool_policy: MCPToolPolicy,
        approval_policy: ApprovalPolicy,
        now: datetime | None = None,
    ) -> ApprovalWaitOutcome:
        """Create or reuse PENDING ApprovalRequest and enter WAITING_APPROVAL.

        Caller must hold FOR UPDATE locks on ``execution`` and ``step`` and have
        already validated runtime preflight + confirmation evidence.
        """
        ts = now or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)

        exec_waiting = execution.status == ExecutionStatus.WAITING_APPROVAL.value
        step_waiting = step.status == StepStatus.WAITING_APPROVAL.value
        if exec_waiting or step_waiting:
            # Valid reuse requires BOTH sides waiting + exactly one PENDING.
            # One-sided WAITING_APPROVAL is atomicity corruption — fail closed.
            return await self.reuse_pending(execution=execution, step=step)

        if execution.status != ExecutionStatus.RUNNING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Execution must be RUNNING to enter WAITING_APPROVAL "
                    f"(got {execution.status})."
                ),
                status_code=409,
            )
        if step.status != StepStatus.READY.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "TOOL Step must be READY to enter WAITING_APPROVAL "
                    f"(got {step.status})."
                ),
                status_code=409,
            )

        if approval_policy.id != tool_policy.approval_policy_id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ApprovalPolicy id does not match ToolPolicy.approval_policy_id.",
                status_code=409,
            )
        if approval_policy.default_expiry_seconds <= 0:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ApprovalPolicy.default_expiry_seconds must be > 0.",
                status_code=409,
            )

        # Another concurrent waiter may already have inserted PENDING under the
        # partial unique index while we waited for row locks.
        existing = await self._requests.find_pending_for_step(
            execution_id=execution.id, step_execution_id=step.id
        )
        if existing is not None:
            self._apply_waiting_state(execution=execution, step=step)
            execution.lock_version += 1
            step.lock_version += 1
            await self._session.flush()
            return ApprovalWaitOutcome(
                execution_id=execution.id,
                step_execution_id=step.id,
                approval_request_id=existing.id,
                context_hash=existing.context_hash,
                reused_existing=True,
            )

        resolved_input = materialize_secret_safe_resolved_input(tool_config.bindings)
        context_snapshot = build_approval_context_snapshot(
            execution=execution,
            step=step,
            tool_policy=tool_policy,
            approval_policy=approval_policy,
            resolved_input=resolved_input,
        )
        context_hash = compute_approval_context_hash(context_snapshot)
        expires_at = ts + timedelta(seconds=approval_policy.default_expiry_seconds)

        request = await self._requests.create_pending(
            execution_id=execution.id,
            step_execution_id=step.id,
            approval_policy_id=approval_policy.id,
            decision_mode=approval_policy.decision_mode,
            required_approvals=approval_policy.required_approvals,
            approval_scope=_copy_scope(approval_policy.approver_scope),
            context_snapshot=context_snapshot,
            context_hash=context_hash,
            requested_at=ts,
            expires_at=expires_at,
            requested_by=execution.requester_id,
        )

        self._apply_waiting_state(execution=execution, step=step)
        # Do not set step.started_at — that marks MCP Attempt start.
        # Do not set finished_at — wait is non-terminal.
        # attempt_count remains unchanged (approval consumes no Attempt).
        step.lock_version += 1
        execution.lock_version += 1
        await self._session.flush()

        return ApprovalWaitOutcome(
            execution_id=execution.id,
            step_execution_id=step.id,
            approval_request_id=request.id,
            context_hash=request.context_hash,
            reused_existing=False,
        )

    async def reuse_pending(
        self, *, execution: Execution, step: ExecutionStep
    ) -> ApprovalWaitOutcome:
        """Idempotent WAITING_APPROVAL reuse (no new ApprovalRequest, no repair).

        Valid only when Execution and Step are both WAITING_APPROVAL and exactly
        one PENDING ApprovalRequest exists. One-sided wait is fail-closed.
        """
        exec_waiting = execution.status == ExecutionStatus.WAITING_APPROVAL.value
        step_waiting = step.status == StepStatus.WAITING_APPROVAL.value
        if exec_waiting != step_waiting:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "One-sided WAITING_APPROVAL is inconsistent "
                    f"(execution={execution.status}, step={step.status})."
                ),
                status_code=409,
            )
        if not (exec_waiting and step_waiting):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Approval wait reuse requires Execution and Step "
                    "WAITING_APPROVAL."
                ),
                status_code=409,
            )

        existing = await self._requests.find_pending_for_step(
            execution_id=execution.id, step_execution_id=step.id
        )
        if existing is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "WAITING_APPROVAL without PENDING ApprovalRequest is inconsistent."
                ),
                status_code=409,
            )
        # Do not rewrite statuses or lease fields — valid wait is already durable.
        return ApprovalWaitOutcome(
            execution_id=execution.id,
            step_execution_id=step.id,
            approval_request_id=existing.id,
            context_hash=existing.context_hash,
            reused_existing=True,
        )

    @staticmethod
    def _apply_waiting_state(*, execution: Execution, step: ExecutionStep) -> None:
        step.status = StepStatus.WAITING_APPROVAL.value
        execution.status = ExecutionStatus.WAITING_APPROVAL.value
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None


def _copy_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    return dict(scope)
