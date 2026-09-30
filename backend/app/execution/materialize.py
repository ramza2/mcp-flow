"""Reusable Execution + ExecutionStep materialization (docs/05 §13).

Persists Execution ``CREATED`` and N ``ExecutionStep`` ``PENDING`` rows from an
already-validated immutable Plan snapshot.

Does **not** queue, dispatch Outbox, claim leases, ready Steps, resolve
bindings, evaluate predicates, create Approvals, expand LOOPs, or call MCP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.complex_plan_validator import StaticComplexPlanValidator
from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, ExecutionStatus, StepStatus
from app.models.execution import Execution, ExecutionStep
from app.repositories.execution import ExecutionRepository
from app.schemas.execution_plan import (
    ApprovalStepConfigV1,
    ComplexToolStepConfigV1,
    ConditionStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    JoinStepConfigV1,
    LoopStepConfigV1,
    compute_plan_hash,
)


@dataclass(frozen=True, slots=True)
class ExecutionMaterializeParams:
    """Caller-supplied lineage + pinned snapshots for materialization."""

    source_type: str
    trigger_type: str
    requester_id: uuid.UUID
    plan_snapshot: dict[str, Any]
    plan_hash: str
    input_snapshot: dict[str, Any]
    policy_snapshot: dict[str, Any]
    requested_at: datetime
    agent_request_id: uuid.UUID | None = None
    agent_version_id: uuid.UUID | None = None
    workflow_version_id: uuid.UUID | None = None
    schedule_occurrence_id: uuid.UUID | None = None
    parent_execution_id: uuid.UUID | None = None
    plan_validation_run_id: uuid.UUID | None = None
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionMaterializeResult:
    execution: Execution
    steps: list[ExecutionStep]


@dataclass(frozen=True, slots=True)
class _StepProjection:
    step_key: str
    step_type: str
    mcp_tool_version_id: uuid.UUID | None
    sequence_hint: int
    step_snapshot: dict[str, Any]


class ExecutionPlanMaterializer:
    """Atomic Execution CREATED + N PENDING ExecutionStep projector."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._executions = ExecutionRepository(session)

    async def materialize(
        self, params: ExecutionMaterializeParams
    ) -> ExecutionMaterializeResult:
        plan, projections = self._prepare(params)

        execution = await self._executions.create_execution(
            source_type=params.source_type,
            trigger_type=params.trigger_type,
            requester_id=params.requester_id,
            agent_request_id=params.agent_request_id,
            agent_version_id=params.agent_version_id,
            workflow_version_id=params.workflow_version_id,
            schedule_occurrence_id=params.schedule_occurrence_id,
            parent_execution_id=params.parent_execution_id,
            plan_validation_run_id=params.plan_validation_run_id,
            status=ExecutionStatus.CREATED.value,
            plan_schema_version=plan.schema_version,
            plan_snapshot=dict(params.plan_snapshot),
            plan_hash=params.plan_hash,
            input_snapshot=dict(params.input_snapshot),
            policy_snapshot=dict(params.policy_snapshot),
            trace_id=params.trace_id,
            requested_at=params.requested_at,
            lock_version=1,
        )

        steps: list[ExecutionStep] = []
        try:
            for projection in projections:
                step = await self._executions.create_step(
                    execution_id=execution.id,
                    step_key=projection.step_key,
                    step_type=projection.step_type,
                    mcp_tool_version_id=projection.mcp_tool_version_id,
                    parent_step_id=None,
                    sequence_hint=projection.sequence_hint,
                    status=StepStatus.PENDING.value,
                    step_snapshot=projection.step_snapshot,
                    lock_version=1,
                )
                steps.append(step)
        except Exception:
            # Fail closed: ensure the caller's TX sees a failed unit of work.
            # Repository flush may have partial rows; caller must rollback.
            raise

        return ExecutionMaterializeResult(execution=execution, steps=steps)

    def _prepare(
        self, params: ExecutionMaterializeParams
    ) -> tuple[ExecutionPlanV1, list[_StepProjection]]:
        if not isinstance(params.plan_snapshot, dict):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="plan_snapshot must be an object.",
                status_code=409,
            )
        if not isinstance(params.plan_hash, str) or not params.plan_hash.strip():
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="plan_hash must be a non-empty string.",
                status_code=409,
            )

        try:
            plan = ExecutionPlanV1.model_validate(params.plan_snapshot)
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"ExecutionPlanV1 validation failed: {exc}",
                status_code=409,
            ) from exc

        recomputed = compute_plan_hash(params.plan_snapshot)
        if recomputed != params.plan_hash:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="plan_hash does not match plan_snapshot.",
                status_code=409,
            )

        # Fail closed even when the caller claims prior validation: reuse the
        # PR #42 StaticComplexPlanValidator as the single static graph contract.
        # Must run before any Execution / ExecutionStep insert.
        static = StaticComplexPlanValidator().validate(plan)
        if not static.ok:
            summary = "; ".join(
                f"{err.code}: {err.message}"
                + (f" (step={err.step_id})" if err.step_id else "")
                for err in static.errors[:5]
            )
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    "static complex-plan validation failed before materialization: "
                    f"{summary}"
                ),
                status_code=409,
                details=[
                    {
                        "code": err.code,
                        "message": err.message,
                        "step_id": err.step_id,
                    }
                    for err in static.errors
                ],
            )

        projections = [
            self._project_step(step, sequence_hint=index)
            for index, step in enumerate(plan.steps)
        ]
        return plan, projections

    def _project_step(
        self, step: ExecutionPlanStep, *, sequence_hint: int
    ) -> _StepProjection:
        snapshot = step.model_dump(mode="json")
        tool_version_id: uuid.UUID | None = None

        try:
            if step.type == AuthorableStepType.TOOL:
                cfg = ComplexToolStepConfigV1.model_validate(step.config)
                tool_version_id = cfg.tool_version_id
            elif step.type == AuthorableStepType.CONDITION:
                ConditionStepConfigV1.model_validate(step.config)
            elif step.type == AuthorableStepType.JOIN:
                JoinStepConfigV1.model_validate(step.config)
            elif step.type == AuthorableStepType.APPROVAL:
                ApprovalStepConfigV1.model_validate(step.config)
            elif step.type == AuthorableStepType.LOOP:
                LoopStepConfigV1.model_validate(step.config)
            else:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=f"unsupported step type={step.type!r}",
                    status_code=409,
                )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    f"invalid {step.type.value} config for step_key={step.id!r}: {exc}"
                ),
                status_code=409,
            ) from exc

        return _StepProjection(
            step_key=step.id,
            step_type=step.type.value,
            mcp_tool_version_id=tool_version_id,
            sequence_hint=sequence_hint,
            step_snapshot=snapshot,
        )
