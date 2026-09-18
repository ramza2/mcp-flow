"""TOOL Step Attempt starter foundation (docs/05 §13.6 / FNC-EXE-004/005 boundary).

Transitions READY → RUNNING and creates StepAttempt STARTED.
Does NOT call MCP, resolve secrets, create ToolCall rows, or wire into Celery claim.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    StepAttemptStatus,
    StepStatus,
)
from app.execution.claim import _as_utc, _normalize_worker_id
from app.execution.runtime_preflight import (
    assert_answered_plan_confirmation,
    assert_current_tool_executable,
)
from app.models.execution import Execution, ExecutionStep
from app.repositories.execution import ExecutionRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.schemas.execution_plan import (
    ExecutionPlanStep,
    ExecutionPlanV1,
    ToolStepConfigV1,
    compute_plan_hash,
)
from app.schemas.parameter_binding import BindingValue

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


def materialize_secret_safe_resolved_input(
    bindings: dict[str, BindingValue],
) -> dict[str, Any]:
    """Project Tool bindings into secret-safe resolved_input.

    LITERAL → value
    SECRET_REF → reference-only object (no secret material)
    Other BindingKinds → fail closed (not supported by AgentRequest foundation).
    """
    resolved: dict[str, Any] = {}
    for key, binding in bindings.items():
        kind = binding.kind
        if kind == BindingKind.LITERAL:
            resolved[key] = binding.value
        elif kind == BindingKind.SECRET_REF:
            resolved[key] = {
                "kind": BindingKind.SECRET_REF.value,
                "secret_id": str(binding.secret_id),
            }
        else:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"Unsupported BindingKind {kind.value!r} for TOOL Step Attempt "
                    "foundation (LITERAL/SECRET_REF only)."
                ),
                status_code=409,
            )
    return resolved


def build_secret_safe_request_snapshot(
    *,
    tool_version_id: uuid.UUID,
    step_key: str,
    bindings: dict[str, BindingValue],
    resolved_input: dict[str, Any],
) -> dict[str, Any]:
    """Attempt request snapshot without raw secret material."""
    safe_bindings: dict[str, Any] = {}
    for key, binding in bindings.items():
        if binding.kind == BindingKind.LITERAL:
            safe_bindings[key] = {
                "kind": BindingKind.LITERAL.value,
                "value": binding.value,
            }
        elif binding.kind == BindingKind.SECRET_REF:
            safe_bindings[key] = {
                "kind": BindingKind.SECRET_REF.value,
                "secret_id": str(binding.secret_id),
            }
        else:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"Unsupported BindingKind {binding.kind.value!r}.",
                status_code=409,
            )
    return {
        "tool_version_id": str(tool_version_id),
        "step_key": step_key,
        "bindings": safe_bindings,
        "resolved_input": resolved_input,
    }


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
    ) -> ToolStepAttemptOutcome:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)

        execution = await self._lock_execution(execution_id)
        self._assert_execution_lease(
            execution, worker_id=worker, lease_token=lease_token, now=ts
        )

        step = await self._lock_step(step_execution_id)
        if step.execution_id != execution.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Step does not belong to Execution.",
                status_code=409,
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
        tool_config = self._validate_tool_lineage(execution, step)
        authz = await assert_current_tool_executable(
            self._session,
            requester_id=execution.requester_id,
            agent_version_id=execution.agent_version_id,
            tool_version_id=tool_config.tool_version_id,
            expected_policy_snapshot=dict(execution.policy_snapshot),
            plan_timeout_seconds=self._plan_timeout_seconds(step),
        )

        # FNC-EXE-009: Approval is out of scope for Attempt foundation.
        # Do not create ApprovalRequest or WAITING_APPROVAL — fail closed.
        if authz.tool_policy.requires_approval:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    "ToolPolicy.requires_approval=true: Attempt foundation "
                    "does not start TOOL Steps (Approval PR scope)."
                ),
                status_code=409,
            )

        if (
            authz.grant.requires_confirmation
            or authz.tool_policy.requires_confirmation
        ):
            await self._assert_confirmation_evidence(execution, authz.policy_snapshot)

        resolved_input = materialize_secret_safe_resolved_input(tool_config.bindings)
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
            tool_version_id=tool_config.tool_version_id,
            step_key=step.step_key,
            bindings=tool_config.bindings,
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
        if execution.source_type != ExecutionSourceType.AGENT_REQUEST.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Attempt foundation supports AgentRequest Executions only.",
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
        self, execution: Execution, step: ExecutionStep
    ) -> ToolStepConfigV1:
        if (
            step.step_type != AuthorableStepType.TOOL.value
            or step.mcp_tool_version_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Only TOOL Steps with mcp_tool_version_id can start Attempts.",
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
        if compute_plan_hash(execution.plan_snapshot) != execution.plan_hash:
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
        expected = plan.steps[0]
        if expected.model_dump(mode="json") != step.step_snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution plan/step snapshot lineage is inconsistent.",
                status_code=409,
            )
        try:
            tool_config = ToolStepConfigV1.model_validate(expected.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution TOOL Step config is invalid.",
                status_code=409,
            ) from exc
        if (
            plan_step.id != step.step_key
            or plan_step.type != AuthorableStepType.TOOL
            or step.mcp_tool_version_id != tool_config.tool_version_id
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="TOOL Step ToolVersion lineage is inconsistent.",
                status_code=409,
            )
        return tool_config

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
