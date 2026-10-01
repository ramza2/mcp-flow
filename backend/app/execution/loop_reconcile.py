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
from app.domain.enums import AuthorableStepType, LoopMode, StepStatus
from app.execution.binding_resolver import RuntimeBindingResolver
from app.execution.loop_runtime import (
    LOOP_COLLECTION_TYPE_MISMATCH,
    PLAN_LIMIT_EXCEEDED,
    child_instances_for_iteration,
    hash_collection,
    iteration_step_key,
    max_materialized_iteration,
    parse_foreach_collection_pin,
    tool_version_for_template,
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

UPSTREAM_LOOP_STOPPED = "UPSTREAM_LOOP_STOPPED"

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
    pinned = parse_foreach_collection_pin(loop_step.resolved_input)
    actual_hash = hash_collection(collection)
    if (
        pinned["collection_hash"] != actual_hash
        or pinned["collection_size"] != len(collection)
    ):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="FOR_EACH collection hash/size drifted from pinned evidence.",
            status_code=409,
        )


def revalidate_foreach_collection_pin(
    *,
    execution: Execution,
    loop_step: ExecutionStep,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
    child_iteration_no: int | None = None,
) -> list[Any]:
    """Resolve collection again and require exact equality with pinned evidence.

    Used before LOOP_CONTEXT rebuild and before every LOOP child TOOL MCP.
    """
    try:
        cfg = LoopStepConfigV1.model_validate(
            ExecutionPlanStep.model_validate(loop_step.step_snapshot).config
        )
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP config invalid during collection pin revalidation.",
            status_code=409,
        ) from exc
    parse_foreach_collection_pin(loop_step.resolved_input)
    collection = resolve_foreach_collection(
        execution=execution,
        loop_step=loop_step,
        plan=plan,
        steps=steps,
        cfg=cfg,
    )
    assert_collection_pin_stable(loop_step=loop_step, collection=collection)
    if child_iteration_no is not None:
        size = len(collection)
        if child_iteration_no < 1 or child_iteration_no > size:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"LOOP child iteration_no={child_iteration_no} is outside "
                    f"pinned collection_size={size}."
                ),
                status_code=409,
            )
    return collection


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
    # Exact budget boundary is exhausted (>=, not only >).
    return elapsed >= float(plan_step.timeout_seconds)


def assert_global_expanded_max_steps(
    *,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
) -> None:
    """Global projected ExecutionStep budget across all RUNNING FOR_EACH LOOPs.

    projected_total =
      current durable ExecutionStep count
      + remaining not-yet-materialized children reserved by every RUNNING LOOP

    Terminal / PENDING LOOPs do not reserve future rows. Malformed pins fail
    closed (mandatory-fatal PLAN_LIMIT_EXCEEDED / RESOURCE_CONFLICT).
    """
    current = len(steps)
    remaining = 0
    plan_by_id = {ps.id: ps for ps in plan.steps}
    for step in steps:
        if (
            step.parent_step_id is not None
            or step.step_type != AuthorableStepType.LOOP.value
            or step.status != StepStatus.RUNNING.value
        ):
            continue
        try:
            pin = parse_foreach_collection_pin(step.resolved_input)
            loop_ps = ExecutionPlanStep.model_validate(step.step_snapshot)
            cfg = LoopStepConfigV1.model_validate(loop_ps.config)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"RUNNING LOOP {step.step_key!r} has malformed pin/config "
                    "for expanded max_steps projection."
                ),
                status_code=409,
            ) from exc
        expected_parent = plan_by_id.get(step.step_key)
        if (
            expected_parent is None
            or expected_parent.model_dump(mode="json") != step.step_snapshot
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"RUNNING LOOP {step.step_key!r} snapshot drift during "
                    "expanded max_steps projection."
                ),
                status_code=409,
            )
        expected_children = pin["collection_size"] * len(cfg.body_step_ids)
        actual_children = sum(1 for s in steps if s.parent_step_id == step.id)
        if actual_children > expected_children:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"LOOP {step.step_key!r} has more child rows "
                    f"({actual_children}) than pinned budget "
                    f"({expected_children})."
                ),
                status_code=409,
            )
        remaining += expected_children - actual_children

    projected = current + remaining
    if projected > plan.limits.max_steps:
        raise AppError(
            code=PLAN_LIMIT_EXCEEDED,
            message=(
                f"Projected runtime step count {projected} exceeds "
                f"limits.max_steps={plan.limits.max_steps}."
            ),
            status_code=409,
        )


def assert_expanded_max_steps(
    *,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
    collection_size: int | None = None,
    body_step_count: int | None = None,
) -> None:
    """Compatibility wrapper → global expanded max_steps projection.

    ``collection_size`` / ``body_step_count`` are ignored; the durable Step set
    (including the newly-pinned RUNNING LOOP) is authoritative.
    """
    del collection_size, body_step_count
    assert_global_expanded_max_steps(plan=plan, steps=steps)


def stop_loop_owned_untouched_children(
    *,
    by_key: Mapping[str, ExecutionStep],
    loop_step: ExecutionStep,
    now: datetime,
) -> None:
    """Terminalize untouched children of a terminal LOOP (timeout / known stop).

    PENDING → SKIPPED / UPSTREAM_LOOP_STOPPED
    unused READY TOOL (no Attempt) → CANCELLED / UPSTREAM_LOOP_STOPPED
    Preserve terminal / in-flight evidence. Unexpected RUNNING → fail closed.
    """
    for step in list(by_key.values()):
        if step.parent_step_id != loop_step.id:
            continue
        if step.status in _ITERATION_TERMINAL:
            continue
        if step.status == StepStatus.PENDING.value:
            step.status = StepStatus.SKIPPED.value
            step.error_code = UPSTREAM_LOOP_STOPPED
            step.error_message = (
                "Owning LOOP stopped before this body Step was started."
            )
            step.finished_at = now
            step.lock_version += 1
            continue
        if (
            step.step_type == AuthorableStepType.TOOL.value
            and step.status == StepStatus.READY.value
            and step.started_at is None
            and step.attempt_count == 0
        ):
            step.status = StepStatus.CANCELLED.value
            step.error_code = UPSTREAM_LOOP_STOPPED
            step.error_message = (
                "Owning LOOP stopped before TOOL Attempt start."
            )
            step.finished_at = now
            step.lock_version += 1
            continue
        if step.status == StepStatus.RUNNING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"LOOP {loop_step.step_key!r} became terminal while body "
                    f"Step {step.step_key!r} is still RUNNING."
                ),
                status_code=409,
            )
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {loop_step.step_key!r} stop cannot clean body Step "
                f"{step.step_key!r} in status {step.status!r}."
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
    return them without creating duplicates. Final guard: never partially
    create an iteration that would cross ``limits.max_steps``.
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

    if len(existing_steps) + len(cfg.body_step_ids) > plan.limits.max_steps:
        raise AppError(
            code=PLAN_LIMIT_EXCEEDED,
            message=(
                f"Materializing iteration {iteration_no} would exceed "
                f"limits.max_steps={plan.limits.max_steps} "
                f"(existing={len(existing_steps)}, "
                f"body={len(cfg.body_step_ids)})."
            ),
            status_code=409,
        )

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
