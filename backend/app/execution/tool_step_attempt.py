"""TOOL Step Attempt starter foundation (docs/05 §13.6 / FNC-EXE-004/005/009).

Transitions READY → RUNNING and creates StepAttempt STARTED, or — when
ToolPolicy.requires_approval — READY → WAITING_APPROVAL with a PENDING
ApprovalRequest and no Attempt/ToolCall/MCP call.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.evidence import find_valid_approved_evidence
from app.approval.wait import ApprovalWaitOutcome, ApprovalWaitService
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    StepAttemptStatus,
    StepStatus,
)
from app.execution.binding_resolver import (
    RuntimeBindingResolver,
    ToolStepLineage,
    binding_kinds_include_dynamic,
)
from app.execution.claim import _as_utc, _normalize_worker_id
from app.execution.lineage import (
    assert_agent_request_plan_step_lineage,
    assert_tool_step_lineage,
    build_secret_safe_request_snapshot,
    materialize_secret_safe_resolved_input,
)
from app.execution.resolved_input_schema import validate_resolved_tool_arguments
from app.execution.runtime_preflight import (
    assert_answered_plan_confirmation,
    assert_current_tool_executable,
)
from app.models.execution import Execution, ExecutionStep
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.plan_validation import PlanValidationRepository

# Re-export for existing imports.
__all__ = [
    "ApprovalWaitOutcome",
    "ToolStepAttemptOutcome",
    "ToolStepAttemptService",
    "build_attempt_idempotency_key",
    "build_secret_safe_request_snapshot",
    "materialize_secret_safe_resolved_input",
]

_ATTEMPT_STARTED = StepAttemptStatus.STARTED.value


@dataclass(frozen=True, slots=True)
class ToolStepAttemptOutcome:
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int
    step_status: str
    attempt_status: str
    replayed: bool


def build_attempt_idempotency_key(
    *, execution_id: uuid.UUID, step_execution_id: uuid.UUID, attempt_no: int
) -> str:
    """Lineage/dedup key for StepAttempt — not remote MCP side-effect idempotency."""
    return (
        f"execution:{execution_id}:step:{step_execution_id}:attempt:{attempt_no}"
    )


class ToolStepAttemptService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._executions = ExecutionRepository(session)

    async def start(
        self,
        *,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
        now: datetime | None = None,
        forbid_new_approval_wait: bool = False,
    ) -> ToolStepAttemptOutcome | ApprovalWaitOutcome:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)

        execution = await self._lock_execution(execution_id)
        step = await self._lock_step(step_execution_id)
        if step.execution_id != execution.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Step does not belong to Execution.",
                status_code=409,
            )

        # Concurrent loser / idempotent wait: both sides must already be waiting.
        exec_waiting = execution.status == ExecutionStatus.WAITING_APPROVAL.value
        step_waiting = step.status == StepStatus.WAITING_APPROVAL.value
        if exec_waiting or step_waiting:
            return await ApprovalWaitService(self._session).reuse_pending(
                execution=execution, step=step
            )

        self._assert_execution_lease(
            execution, worker_id=worker, lease_token=lease_token, now=ts
        )

        # Idempotent replay: same worker/lease against already-RUNNING Step.
        if step.status == StepStatus.RUNNING.value:
            return await self._replay_started(
                execution=execution, step=step, worker_id=worker
            )

        if step.status != StepStatus.READY.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"TOOL Step must be READY to start an Attempt "
                    f"(got {step.status})."
                ),
                status_code=409,
            )

        assert execution.agent_version_id is not None
        steps = await self._executions.list_steps(execution.id)
        lineage = assert_tool_step_lineage(execution, step, steps=steps)
        authz = await assert_current_tool_executable(
            self._session,
            requester_id=execution.requester_id,
            agent_version_id=execution.agent_version_id,
            tool_version_id=lineage.tool_version_id,
            expected_policy_snapshot=dict(execution.policy_snapshot),
            plan_timeout_seconds=self._plan_timeout_seconds(step),
        )

        if (
            authz.grant.requires_confirmation
            or authz.tool_policy.requires_confirmation
        ):
            if binding_kinds_include_dynamic(lineage.bindings):
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        "PLAN_CONFIRMATION with dynamic Plan bindings "
                        "(PLAN_INPUT/STEP_OUTPUT/EXECUTION_CONTEXT/LOOP_CONTEXT) "
                        "is not supported in this slice."
                    ),
                    status_code=409,
                )
            await self._assert_confirmation_evidence(execution, authz.policy_snapshot)

        # FNC-EXE-009 / FNC-APR-002/004: ToolPolicy approval before Attempt.
        # A) no approval history → create PENDING wait
        # B) exact APPROVED evidence for current context → continue to Attempt
        # C) historical APPROVED but context mismatch → fail closed (no new PENDING)
        if authz.tool_policy.requires_approval:
            if binding_kinds_include_dynamic(lineage.bindings):
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        "ToolPolicy requires_approval with dynamic Plan bindings "
                        "is not supported in this slice."
                    ),
                    status_code=409,
                )
            tool_config = assert_agent_request_plan_step_lineage(
                execution, step, steps=steps
            )
            if authz.approval_policy is None:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message="requires_approval인데 ApprovalPolicy 없음.",
                    status_code=409,
                )
            evidence = await find_valid_approved_evidence(
                self._session,
                execution=execution,
                step=step,
                tool_policy=authz.tool_policy,
                approval_policy=authz.approval_policy,
            )
            if evidence is None:
                if forbid_new_approval_wait:
                    raise AppError(
                        code="DAG_WAIT_UNSUPPORTED",
                        message=(
                            "Multi-Step TOOL/JOIN DAG cannot create "
                            "Execution-level WAITING_APPROVAL; branch-local "
                            "suspension is out of scope for this runtime slice."
                        ),
                        status_code=409,
                    )
                return await ApprovalWaitService(self._session).enter_for_tool_step(
                    execution=execution,
                    step=step,
                    tool_config=tool_config,
                    tool_policy=authz.tool_policy,
                    approval_policy=authz.approval_policy,
                    now=ts,
                )
            # Approval satisfied for exact current context — continue to Attempt.

        resolved_input = RuntimeBindingResolver().resolve(
            execution=execution,
            step=step,
            steps=steps,
            bindings=lineage.bindings,
            plan=lineage.plan,
        )
        # Retry/replay pinning: never silently rewrite resolved_input.
        if step.resolved_input is not None and step.resolved_input != resolved_input:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Deterministic Binding resolution drifted from pinned "
                    "Step.resolved_input."
                ),
                status_code=409,
            )

        tool_version = await MCPToolRepository(self._session).get_version(
            lineage.tool_version_id
        )
        if tool_version is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ToolVersion not found for resolved-input schema validation.",
                status_code=409,
            )
        validate_resolved_tool_arguments(
            input_schema=tool_version.input_schema,
            resolved_input=resolved_input,
        )

        next_attempt_no = step.attempt_count + 1
        if next_attempt_no < 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="attempt_count invariant broken.",
                status_code=409,
            )
        idem_key = build_attempt_idempotency_key(
            execution_id=execution.id,
            step_execution_id=step.id,
            attempt_no=next_attempt_no,
        )
        request_snapshot = build_secret_safe_request_snapshot(
            tool_version_id=lineage.tool_version_id,
            step_key=step.step_key,
            bindings=lineage.bindings,
            resolved_input=resolved_input,
        )

        # FOR UPDATE on Execution+Step serializes concurrent starts: the loser
        # waits, then either sees RUNNING and replays, or still sees READY and
        # creates the Attempt. No IntegrityError reconcile / session.rollback —
        # that would abort the caller's broader transaction.
        step.status = StepStatus.RUNNING.value
        if step.started_at is None:
            step.started_at = ts
        step.attempt_count = next_attempt_no
        step.resolved_input = resolved_input
        step.lock_version += 1

        attempt = await self._executions.create_attempt(
            step_execution_id=step.id,
            attempt_no=next_attempt_no,
            status=_ATTEMPT_STARTED,
            worker_id=worker,
            lease_expires_at=execution.lease_expires_at,
            idempotency_key=idem_key,
            request_snapshot=request_snapshot,
            started_at=ts,
        )
        await self._session.flush()

        return ToolStepAttemptOutcome(
            execution_id=execution.id,
            step_execution_id=step.id,
            attempt_id=attempt.id,
            attempt_no=attempt.attempt_no,
            step_status=step.status,
            attempt_status=attempt.status,
            replayed=False,
        )

    async def _lock_execution(self, execution_id: uuid.UUID) -> Execution:
        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=404,
            )
        return execution

    async def _lock_step(self, step_execution_id: uuid.UUID) -> ExecutionStep:
        stmt = (
            select(ExecutionStep)
            .where(ExecutionStep.id == step_execution_id)
            .with_for_update()
        )
        step = (await self._session.execute(stmt)).scalar_one_or_none()
        if step is None:
            raise AppError(
                code="NOT_FOUND",
                message="ExecutionStep not found.",
                status_code=404,
            )
        return step

    def _assert_execution_lease(
        self,
        execution: Execution,
        *,
        worker_id: str,
        lease_token: uuid.UUID,
        now: datetime,
    ) -> None:
        if execution.status != ExecutionStatus.RUNNING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution must be RUNNING to start a TOOL Step Attempt.",
                status_code=409,
            )
        if execution.source_type not in {
            ExecutionSourceType.AGENT_REQUEST.value,
            ExecutionSourceType.MANUAL_TOOL_TEST.value,
        }:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Attempt foundation supports AGENT_REQUEST / "
                    "MANUAL_TOOL_TEST Executions only."
                ),
                status_code=409,
            )
        if execution.agent_version_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution.agent_version_id is required.",
                status_code=409,
            )
        if (
            execution.worker_id != worker_id
            or execution.lease_token != lease_token
            or execution.lease_expires_at is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution worker/lease mismatch.",
                status_code=409,
            )
        if _as_utc(execution.lease_expires_at) <= _as_utc(now):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution lease has expired.",
                status_code=409,
            )

    def _validate_tool_lineage(
        self,
        execution: Execution,
        step: ExecutionStep,
        steps: Sequence[ExecutionStep] | None = None,
    ) -> ToolStepLineage:
        return assert_tool_step_lineage(execution, step, steps=steps)

    def _plan_timeout_seconds(self, step: ExecutionStep) -> int | None:
        timeout = step.step_snapshot.get("timeout_seconds")
        if timeout is None:
            return None
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Step timeout_seconds is invalid.",
                status_code=409,
            )
        return timeout

    async def _replay_started(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        worker_id: str,
    ) -> ToolStepAttemptOutcome:
        attempt = await self._executions.get_started_attempt(step.id)
        if attempt is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="RUNNING Step has no STARTED Attempt.",
                status_code=409,
            )
        if attempt.worker_id != worker_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="RUNNING Step Attempt worker mismatch.",
                status_code=409,
            )
        return ToolStepAttemptOutcome(
            execution_id=execution.id,
            step_execution_id=step.id,
            attempt_id=attempt.id,
            attempt_no=attempt.attempt_no,
            step_status=step.status,
            attempt_status=attempt.status,
            replayed=True,
        )

    async def _assert_confirmation_evidence(
        self,
        execution: Execution,
        policy_snapshot: dict[str, Any],
    ) -> None:
        if (
            execution.agent_request_id is None
            or execution.plan_validation_run_id is None
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="PLAN_CONFIRMATION evidence lineage missing on Execution.",
                status_code=409,
            )
        validation = await PlanValidationRepository(self._session).get_by_id(
            execution.plan_validation_run_id
        )
        if validation is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Pinned PlanValidationRun not found for confirmation check.",
                status_code=409,
            )
        await assert_answered_plan_confirmation(
            self._session,
            agent_request_id=execution.agent_request_id,
            requester_id=execution.requester_id,
            plan_generation_run_id=validation.plan_generation_run_id,
            plan_hash=execution.plan_hash,
            policy_snapshot=policy_snapshot,
        )
