"""Immutable Execution / Step / Attempt lineage checks for recovery + runner replay.

Fresh READY Attempt starts keep ToolStepAttemptService validation.
RESUME_ATTEMPT / replayed runner paths must re-assert pinned plan/input lineage
before any remote tools/call.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    StepAttemptStatus,
    StepStatus,
)
from app.execution.binding_resolver import (
    RuntimeBindingResolver,
    ToolStepLineage,
    serialize_plan_bindings,
)
from app.execution.loop_runtime import (
    build_loop_body_ownership,
    iteration_step_key,
    parse_foreach_collection_pin,
    template_id_from_step_snapshot,
)
from app.models.execution import Execution, ExecutionStep, StepAttempt
from app.schemas.execution_plan import (
    ComplexToolStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    LoopStepConfigV1,
    ToolStepConfigV1,
    compute_plan_hash,
)
from app.schemas.parameter_binding import BindingValue
from app.schemas.plan_binding import PlanBindingValue, parse_plan_binding_value

_LINEAGE_SOURCES = frozenset(
    {
        ExecutionSourceType.AGENT_REQUEST.value,
        ExecutionSourceType.MANUAL_TOOL_TEST.value,
        ExecutionSourceType.WORKFLOW_VERSION.value,
    }
)


def materialize_secret_safe_resolved_input(
    bindings: dict[str, BindingValue],
) -> dict[str, Any]:
    """Project AgentRequest Tool bindings into secret-safe resolved_input.

    LITERAL → value
    SECRET_REF → reference-only object (no secret material)
    Other BindingKinds → fail closed (AgentRequest static path only).
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
    bindings: dict[str, BindingValue] | dict[str, PlanBindingValue],
    resolved_input: dict[str, Any],
) -> dict[str, Any]:
    """Attempt request snapshot without raw secret material."""
    safe_bindings: dict[str, Any] = {}
    for key, binding in bindings.items():
        try:
            dumped = binding.model_dump(mode="json")  # type: ignore[union-attr]
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"Unsupported BindingKind {getattr(binding, 'kind', None)!r}.",
                status_code=409,
            ) from exc
        if not isinstance(dumped, dict) or "kind" not in dumped:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=f"Unsupported binding payload for key {key!r}.",
                status_code=409,
            )
        safe_bindings[key] = dumped
    return {
        "tool_version_id": str(tool_version_id),
        "step_key": step_key,
        "bindings": safe_bindings,
        "resolved_input": resolved_input,
    }


def assert_tool_step_lineage(
    execution: Execution,
    step: ExecutionStep,
    steps: Sequence[ExecutionStep] | None = None,
) -> ToolStepLineage:
    """Source-aware TOOL Step lineage.

    - AGENT_REQUEST top-level: ToolStepConfigV1 only (LITERAL / SECRET_REF)
    - MANUAL_TOOL_TEST top-level: ComplexToolStepConfigV1 (full Plan BindingKind set)
    - LOOP body TOOL child: ComplexToolStepConfigV1 (requires ``steps``)
    """
    if execution.source_type not in _LINEAGE_SOURCES:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Replay lineage supports AGENT_REQUEST / MANUAL_TOOL_TEST / "
                "WORKFLOW_VERSION only."
            ),
            status_code=409,
        )

    try:
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
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
    if not plan.steps:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan must contain at least one Step.",
            status_code=409,
        )

    if step.parent_step_id is None:
        return _assert_top_level_tool_step_lineage(
            execution=execution, step=step, plan=plan
        )
    return _assert_loop_child_tool_step_lineage(
        execution=execution, step=step, plan=plan, steps=steps
    )


def _assert_top_level_tool_step_lineage(
    *,
    execution: Execution,
    step: ExecutionStep,
    plan: ExecutionPlanV1,
) -> ToolStepLineage:
    if (
        step.step_type != AuthorableStepType.TOOL.value
        or step.mcp_tool_version_id is None
        or step.parent_step_id is not None
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ExecutionStep is inconsistent with TOOL orchestration foundation.",
            status_code=409,
        )

    try:
        plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan/step snapshot is invalid.",
            status_code=409,
        ) from exc

    expected = next((ps for ps in plan.steps if ps.id == step.step_key), None)
    if expected is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"No plan step matches step_key={step.step_key!r}.",
            status_code=409,
        )
    if expected.model_dump(mode="json") != step.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan/step snapshot lineage is inconsistent.",
            status_code=409,
        )
    if (
        plan_step.id != step.step_key
        or plan_step.id != expected.id
        or plan_step.type != AuthorableStepType.TOOL
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="TOOL Step foundation lineage is inconsistent.",
            status_code=409,
        )

    agent_request_static_only = (
        execution.source_type == ExecutionSourceType.AGENT_REQUEST.value
    )
    if agent_request_static_only:
        try:
            tool_config = ToolStepConfigV1.model_validate(expected.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution TOOL Step config is invalid.",
                status_code=409,
            ) from exc
        if step.mcp_tool_version_id != tool_config.tool_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="TOOL Step foundation lineage is inconsistent.",
                status_code=409,
            )
        bindings = _agent_bindings_as_plan(tool_config.bindings)
        return ToolStepLineage(
            tool_version_id=tool_config.tool_version_id,
            bindings=bindings,
            plan=plan,
            plan_step=plan_step,
            agent_request_static_only=True,
        )

    try:
        complex_cfg = ComplexToolStepConfigV1.model_validate(expected.config)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution TOOL Step config is invalid.",
            status_code=409,
        ) from exc
    if step.mcp_tool_version_id != complex_cfg.tool_version_id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="TOOL Step foundation lineage is inconsistent.",
            status_code=409,
        )
    return ToolStepLineage(
        tool_version_id=complex_cfg.tool_version_id,
        bindings=dict(complex_cfg.bindings),
        plan=plan,
        plan_step=plan_step,
        agent_request_static_only=False,
    )


def _assert_loop_child_tool_step_lineage(
    *,
    execution: Execution,
    step: ExecutionStep,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep] | None,
) -> ToolStepLineage:
    """TOOL Step lineage for a flat FOR_EACH LOOP body instance."""
    if step.step_type != AuthorableStepType.TOOL.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child lineage requires step_type TOOL.",
            status_code=409,
        )
    if step.mcp_tool_version_id is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL Step requires mcp_tool_version_id.",
            status_code=409,
        )
    if step.iteration_no is None or step.iteration_no < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL Step requires iteration_no >= 1.",
            status_code=409,
        )
    if steps is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL lineage requires the Execution Step set.",
            status_code=409,
        )

    parent = next((s for s in steps if s.id == step.parent_step_id), None)
    if parent is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL parent Step is missing.",
            status_code=409,
        )
    if parent.execution_id != execution.id or step.execution_id != execution.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL parent/child Execution mismatch.",
            status_code=409,
        )
    if parent.parent_step_id is not None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL parent must be a top-level LOOP Step.",
            status_code=409,
        )
    if parent.step_type != AuthorableStepType.LOOP.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL parent Step must be type LOOP.",
            status_code=409,
        )
    if parent.status != StepStatus.RUNNING.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP child TOOL parent must be RUNNING "
                f"(got {parent.status!r})."
            ),
            status_code=409,
        )

    try:
        parent_plan_step = ExecutionPlanStep.model_validate(parent.step_snapshot)
        plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
        loop_cfg = LoopStepConfigV1.model_validate(parent_plan_step.config)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan/step snapshot is invalid.",
            status_code=409,
        ) from exc

    if parent_plan_step.type != AuthorableStepType.LOOP:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL parent snapshot type must be LOOP.",
            status_code=409,
        )
    if parent.step_key != parent_plan_step.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP parent step_key does not match Plan LOOP id.",
            status_code=409,
        )
    expected_parent = next(
        (ps for ps in plan.steps if ps.id == parent.step_key), None
    )
    if expected_parent is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"No plan step matches LOOP step_key={parent.step_key!r}.",
            status_code=409,
        )
    if expected_parent.model_dump(mode="json") != parent.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP parent plan/step snapshot lineage is inconsistent.",
            status_code=409,
        )

    ownership = build_loop_body_ownership(plan)
    if plan_step.id not in loop_cfg.body_step_ids:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"TOOL template {plan_step.id!r} is not in LOOP "
                f"{parent_plan_step.id!r} body_step_ids."
            ),
            status_code=409,
        )
    if ownership.body_to_loop.get(plan_step.id) != parent_plan_step.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"TOOL template {plan_step.id!r} ownership does not match "
                f"parent LOOP {parent_plan_step.id!r}."
            ),
            status_code=409,
        )

    expected = next((ps for ps in plan.steps if ps.id == plan_step.id), None)
    if expected is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"No plan step matches LOOP body template {plan_step.id!r}.",
            status_code=409,
        )
    if expected.model_dump(mode="json") != step.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan/step snapshot lineage is inconsistent.",
            status_code=409,
        )
    if plan_step.type != AuthorableStepType.TOOL or plan_step.id != expected.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL Step foundation lineage is inconsistent.",
            status_code=409,
        )

    expected_key = iteration_step_key(
        parent_step_id=parent.id,
        iteration_no=step.iteration_no,
        template_step_id=plan_step.id,
    )
    if step.step_key != expected_key:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP child step_key {step.step_key!r} does not match "
                f"deterministic key {expected_key!r}."
            ),
            status_code=409,
        )
    if template_id_from_step_snapshot(step) != plan_step.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL template identity is inconsistent.",
            status_code=409,
        )

    try:
        complex_cfg = ComplexToolStepConfigV1.model_validate(expected.config)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution TOOL Step config is invalid.",
            status_code=409,
        ) from exc
    if step.mcp_tool_version_id != complex_cfg.tool_version_id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="TOOL Step foundation lineage is inconsistent.",
            status_code=409,
        )

    # Pre-MCP: revalidate FOR_EACH collection pin / WHILE predicate gate
    # even for LITERAL-only body TOOLs.
    # Lazy import avoids import cycles with loop_reconcile → binding_resolver.
    from app.domain.enums import LoopMode
    from app.execution.loop_reconcile import (
        revalidate_foreach_collection_pin,
        revalidate_while_predicate_gate,
    )

    if loop_cfg.mode == LoopMode.FOR_EACH:
        parse_foreach_collection_pin(parent.resolved_input)
        revalidate_foreach_collection_pin(
            execution=execution,
            loop_step=parent,
            plan=plan,
            steps=steps,
            child_iteration_no=step.iteration_no,
        )
    elif loop_cfg.mode == LoopMode.WHILE:
        revalidate_while_predicate_gate(
            execution=execution,
            loop_step=parent,
            plan=plan,
            steps=steps,
            child_iteration_no=step.iteration_no,
        )
    else:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP child TOOL parent mode {loop_cfg.mode.value!r} "
                "unsupported for pre-MCP gate."
            ),
            status_code=409,
        )

    # LOOP body TOOL always uses complex Plan bindings (even under AGENT_REQUEST).
    return ToolStepLineage(
        tool_version_id=complex_cfg.tool_version_id,
        bindings=dict(complex_cfg.bindings),
        plan=plan,
        plan_step=plan_step,
        agent_request_static_only=False,
    )


def assert_agent_request_plan_step_lineage(
    execution: Execution,
    step: ExecutionStep,
    steps: Sequence[ExecutionStep] | None = None,
) -> ToolStepConfigV1:
    """AgentRequest-compatible TOOL lineage (LITERAL / SECRET_REF only).

    MANUAL_TOOL_TEST with only LITERAL/SECRET_REF still returns ToolStepConfigV1.
    Dynamic Plan bindings on MANUAL_TOOL_TEST raise — callers needing those must
    use ``assert_tool_step_lineage``.
    """
    lineage = assert_tool_step_lineage(execution, step, steps=steps)
    try:
        return ToolStepConfigV1.model_validate(
            {
                "tool_version_id": str(lineage.tool_version_id),
                "bindings": serialize_plan_bindings(lineage.bindings),
            }
        )
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "TOOL Step config is not AgentRequest-compatible "
                "(LITERAL/SECRET_REF only)."
            ),
            status_code=409,
        ) from exc


def _agent_bindings_as_plan(
    bindings: dict[str, BindingValue],
) -> dict[str, PlanBindingValue]:
    out: dict[str, PlanBindingValue] = {}
    for key, binding in bindings.items():
        out[key] = parse_plan_binding_value(binding.model_dump(mode="json"))
    return out


def assert_started_attempt_replay_lineage(
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    lineage: ToolStepLineage,
    steps: Sequence[ExecutionStep],
    worker_id: str | None = None,
) -> None:
    """Validate STARTED Attempt snapshots against deterministic Binding recompute."""
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

    expected_resolved = RuntimeBindingResolver().resolve(
        execution=execution,
        step=step,
        steps=steps,
        bindings=lineage.bindings,
        plan=lineage.plan,
    )
    if step.resolved_input != expected_resolved:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Step.resolved_input does not match deterministic Binding "
                "resolution (retry drift)."
            ),
            status_code=409,
        )

    expected_request = build_secret_safe_request_snapshot(
        tool_version_id=lineage.tool_version_id,
        step_key=step.step_key,
        bindings=lineage.bindings,
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
    steps: Sequence[ExecutionStep],
    worker_id: str | None = None,
) -> ToolStepLineage:
    """Full immutable lineage gate for RESUME_ATTEMPT / runner replay."""
    lineage = assert_tool_step_lineage(execution, step, steps=steps)
    assert_started_attempt_replay_lineage(
        execution=execution,
        step=step,
        attempt=attempt,
        lineage=lineage,
        steps=steps,
        worker_id=worker_id,
    )
    return lineage
