"""FOR_EACH LOOP local reconciliation helpers (docs/04 §9.5).

Orchestrator-owned: no MCP. Creates durable iteration ExecutionStep rows
under Execution row lock. WHILE / nested LOOP / body APPROVAL are rejected
by ``assert_flat_foreach_runtime_compatible`` before body work.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.core.errors import AppError
from app.domain.enums import LoopMode, StepStatus
from app.execution.binding_resolver import RuntimeBindingResolver
from app.execution.loop_runtime import (
    LOOP_COLLECTION_TYPE_MISMATCH,
    PLAN_LIMIT_EXCEEDED,
    child_instances_for_iteration,
    hash_collection,
    iteration_step_key,
    max_materialized_iteration,
    projected_expanded_step_count,
    tool_version_for_template,
    top_level_plan_steps,
)
from app.models.execution import Execution, ExecutionStep
from app.repositories.execution import ExecutionRepository
from app.schemas.execution_plan import (
    ExecutionPlanStep,
    ExecutionPlanV1,
    LoopStepConfigV1,
)
from app.schemas.plan_binding import PlanLoopContextBinding

logger = logging.getLogger(__name__)

_ITERATION_TERMINAL = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.SKIPPED.value,
        StepStatus.CANCELLED.value,
        StepStatus.UNKNOWN_OUTCOME.value,
    }
)


def runtime_plan_step(
    step: ExecutionStep, plan_by_id: Mapping[str, ExecutionPlanStep]
) -> ExecutionPlanStep:
    """Resolve immutable Plan Step for top-level or body instance rows."""
    try:
        parsed = ExecutionPlanStep.model_validate(step.step_snapshot)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"step_snapshot invalid for {step.step_key!r}.",
            status_code=409,
        ) from exc
    expected = plan_by_id.get(parsed.id)
    if expected is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"No Plan Step for template {parsed.id!r}.",
            status_code=409,
        )
    if expected.model_dump(mode="json") != step.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"step_snapshot is not an exact projection of immutable "
                f"Plan Step {parsed.id!r}."
            ),
            status_code=409,
        )
    return expected


def resolve_foreach_collection(
    *,
    execution: Execution,
    loop_step: ExecutionStep,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
    cfg: LoopStepConfigV1,
) -> list[Any]:
    """Resolve FOR_EACH collection; fail closed on type / LOOP_CONTEXT."""
    if cfg.mode != LoopMode.FOR_EACH or cfg.collection is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="FOR_EACH LOOP requires a collection binding.",
            status_code=409,
        )
    if isinstance(cfg.collection, PlanLoopContextBinding):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="LOOP collection must not use LOOP_CONTEXT in this slice.",
            status_code=409,
        )
    resolver = RuntimeBindingResolver()
    ancestors = resolver.transitive_ancestors(plan)
    value = resolver.resolve_binding(
        binding=cfg.collection,
        execution=execution,
        owning_step=loop_step,
        by_key={s.step_key: s for s in steps},
        plan=plan,
        ancestors=ancestors,
        missing_ok=False,
    )
    if not isinstance(value, list):
        raise AppError(
            code=LOOP_COLLECTION_TYPE_MISMATCH,
            message="FOR_EACH collection must resolve to a JSON array/list.",
            status_code=409,
        )
    return value


def pin_collection_evidence(
    *,
    loop_step: ExecutionStep,
    collection: Sequence[Any],
) -> dict[str, Any]:
    evidence = {
        "mode": LoopMode.FOR_EACH.value,
        "collection_hash": hash_collection(collection),
        "collection_size": len(collection),
    }
    loop_step.resolved_input = evidence
    return evidence


def assert_collection_pin_stable(
    *,
    loop_step: ExecutionStep,
    collection: Sequence[Any],
) -> None:
    pinned = loop_step.resolved_input or {}
    actual_hash = hash_collection(collection)
    if (
        pinned.get("collection_hash") != actual_hash
        or pinned.get("collection_size") != len(collection)
    ):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="FOR_EACH collection hash/size drifted from pinned evidence.",
            status_code=409,
        )


def loop_timeout_exceeded(loop_step: ExecutionStep, *, now: datetime) -> bool:
    if loop_step.started_at is None:
        return False
    started = loop_step.started_at
    if started.tzinfo is None:
        from datetime import UTC

        started = started.replace(tzinfo=UTC)
    elapsed = (now - started).total_seconds()
    try:
        plan_step = ExecutionPlanStep.model_validate(loop_step.step_snapshot)
    except Exception:
        return False
    return elapsed > float(plan_step.timeout_seconds)


def assert_expanded_max_steps(
    *,
    plan: ExecutionPlanV1,
    collection_size: int,
    body_step_count: int,
) -> None:
    top_count = len(top_level_plan_steps(plan))
    projected = projected_expanded_step_count(
        top_level_count=top_count,
        collection_size=collection_size,
        body_step_count=body_step_count,
    )
    if projected > plan.limits.max_steps:
        raise AppError(
            code=PLAN_LIMIT_EXCEEDED,
            message=(
                f"Projected runtime step count {projected} exceeds "
                f"limits.max_steps={plan.limits.max_steps}."
            ),
            status_code=409,
        )


async def materialize_iteration(
    *,
    executions: ExecutionRepository,
    execution: Execution,
    loop_step: ExecutionStep,
    plan: ExecutionPlanV1,
    cfg: LoopStepConfigV1,
    iteration_no: int,
    existing_steps: Sequence[ExecutionStep],
) -> list[ExecutionStep]:
    """Create one PENDING ExecutionStep per body template for iteration_no.

    Idempotent under Execution lock: if rows already exist for this iteration,
    return them without creating duplicates.
    """
    if iteration_no < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="iteration_no must be >= 1.",
            status_code=409,
        )
    existing = child_instances_for_iteration(
        steps=existing_steps,
        parent_step_id=loop_step.id,
        iteration_no=iteration_no,
    )
    if existing:
        if len(existing) != len(cfg.body_step_ids):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Partial LOOP iteration {iteration_no} materialization "
                    "detected."
                ),
                status_code=409,
            )
        return existing

    plan_by_id = {ps.id: ps for ps in plan.steps}
    created: list[ExecutionStep] = []
    # sequence_hint: after existing max, stable within iteration by body order
    base_hint = max((s.sequence_hint for s in existing_steps), default=0) + 1
    for offset, body_id in enumerate(cfg.body_step_ids):
        template = plan_by_id[body_id]
        key = iteration_step_key(
            parent_step_id=loop_step.id,
            iteration_no=iteration_no,
            template_step_id=body_id,
        )
        row = await executions.create_step(
            execution_id=execution.id,
            step_key=key,
            step_type=template.type.value,
            mcp_tool_version_id=tool_version_for_template(template),
            parent_step_id=loop_step.id,
            sequence_hint=base_hint + offset,
            status=StepStatus.PENDING.value,
            step_snapshot=template.model_dump(mode="json"),
            lock_version=1,
            iteration_no=iteration_no,
        )
        created.append(row)
        logger.info(
            "loop materialize iteration execution_id=%s loop=%s iter=%s "
            "template=%s step_key=%s",
            execution.id,
            loop_step.step_key,
            iteration_no,
            body_id,
            key,
        )
    return created


def iteration_complete(
    *,
    steps: Sequence[ExecutionStep],
    parent_step_id: uuid.UUID,
    iteration_no: int,
    body_step_count: int,
) -> bool:
    children = child_instances_for_iteration(
        steps=steps,
        parent_step_id=parent_step_id,
        iteration_no=iteration_no,
    )
    if len(children) != body_step_count:
        return False
    return all(c.status in _ITERATION_TERMINAL for c in children)


def succeed_loop(
    loop_step: ExecutionStep,
    *,
    collection_size: int,
    iterations_completed: int,
    now: datetime,
) -> None:
    loop_step.status = StepStatus.SUCCEEDED.value
    loop_step.result_inline = {
        "mode": LoopMode.FOR_EACH.value,
        "iterations_completed": iterations_completed,
        "collection_size": collection_size,
    }
    loop_step.error_code = None
    loop_step.error_message = None
    loop_step.finished_at = now
    loop_step.lock_version += 1


def fail_loop_known(
    loop_step: ExecutionStep,
    *,
    status: str,
    error_code: str,
    error_message: str,
    now: datetime,
) -> None:
    loop_step.status = status
    loop_step.error_code = error_code
    loop_step.error_message = error_message
    loop_step.finished_at = now
    loop_step.lock_version += 1


def active_iteration_no(
    *,
    steps: Sequence[ExecutionStep],
    parent_step_id: uuid.UUID,
) -> int:
    return max_materialized_iteration(
        steps=steps, parent_step_id=parent_step_id
    )
