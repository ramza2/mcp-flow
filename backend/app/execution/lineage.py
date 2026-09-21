"""Immutable Execution / Step / Attempt lineage checks for recovery + runner replay.

Fresh READY Attempt starts keep ToolStepAttemptService validation.
RESUME_ATTEMPT / replayed runner paths must re-assert pinned plan/input lineage
before any remote tools/call.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    StepAttemptStatus,
)
from app.models.execution import Execution, ExecutionStep, StepAttempt
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    ExecutionPlanStep,
    ExecutionPlanV1,
    ToolStepConfigV1,
    compute_plan_hash,
)
from app.schemas.parameter_binding import BindingValue


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


def assert_agent_request_plan_step_lineage(
    execution: Execution, step: ExecutionStep
) -> ToolStepConfigV1:
    """Validate pinned plan/step snapshots for AgentRequest single-TOOL foundation."""
    if execution.source_type != ExecutionSourceType.AGENT_REQUEST.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Replay lineage supports AgentRequest Executions only.",
            status_code=409,
        )
    if (
        step.step_type != AuthorableStepType.TOOL.value
        or step.mcp_tool_version_id is None
        or step.parent_step_id is not None
        or step.step_key != DETERMINISTIC_TOOL_STEP_ID
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ExecutionStep is inconsistent with AgentRequest TOOL foundation.",
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

    if execution.plan_schema_version != plan.schema_version:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan_schema_version does not match plan snapshot.",
            status_code=409,
        )
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
        or plan_step.id != expected.id
        or plan_step.type != AuthorableStepType.TOOL
        or plan_step.depends_on != []
        or plan_step.when is not None
        or step.mcp_tool_version_id != tool_config.tool_version_id
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="TOOL Step foundation lineage is inconsistent.",
            status_code=409,
        )
    return tool_config


def assert_started_attempt_replay_lineage(
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    tool_config: ToolStepConfigV1,
    worker_id: str | None = None,
) -> None:
    """Validate STARTED Attempt input snapshots against pinned Tool bindings.

    SECRET_REF comparisons are reference-only; secrets are never resolved.
    """
    del execution  # reserved for future execution-scoped attempt invariants
    if attempt.status != StepAttemptStatus.STARTED.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Replay Attempt must be STARTED.",
            status_code=409,
        )
    if attempt.step_execution_id != step.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="StepAttempt lineage does not match Step.",
            status_code=409,
        )
    if attempt.attempt_no != step.attempt_count:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="StepAttempt attempt_no does not match Step.attempt_count.",
            status_code=409,
        )
    if worker_id is not None and attempt.worker_id != worker_id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="RUNNING Step Attempt worker mismatch.",
            status_code=409,
        )

    expected_resolved = materialize_secret_safe_resolved_input(tool_config.bindings)
    if step.resolved_input != expected_resolved:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Step.resolved_input does not match pinned Tool bindings.",
            status_code=409,
        )

    expected_request = build_secret_safe_request_snapshot(
        tool_version_id=tool_config.tool_version_id,
        step_key=step.step_key,
        bindings=tool_config.bindings,
        resolved_input=expected_resolved,
    )
    if attempt.request_snapshot != expected_request:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="StepAttempt.request_snapshot does not match pinned Tool bindings.",
            status_code=409,
        )


def assert_resume_attempt_lineage(
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    worker_id: str | None = None,
) -> ToolStepConfigV1:
    """Full immutable lineage gate for RESUME_ATTEMPT / runner replay."""
    tool_config = assert_agent_request_plan_step_lineage(execution, step)
    assert_started_attempt_replay_lineage(
        execution=execution,
        step=step,
        attempt=attempt,
        tool_config=tool_config,
        worker_id=worker_id,
    )
    return tool_config
