"""Flat FOR_EACH LOOP runtime helpers (docs/04 §9.5).

Body templates are immutable Plan definitions. Runtime materializes durable
iteration ExecutionStep instances with deterministic keys.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, LoopMode
from app.models.execution import ExecutionStep
from app.schemas.execution_plan import (
    ComplexToolStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    LoopStepConfigV1,
)

LOOP_RUNTIME_UNSUPPORTED = "LOOP_RUNTIME_UNSUPPORTED"
LOOP_COLLECTION_TYPE_MISMATCH = "LOOP_COLLECTION_TYPE_MISMATCH"
LOOP_MAX_ITERATIONS_EXCEEDED = "LOOP_MAX_ITERATIONS_EXCEEDED"
LOOP_TIMEOUT = "LOOP_TIMEOUT"
PLAN_LIMIT_EXCEEDED = "PLAN_LIMIT_EXCEEDED"


@dataclass(frozen=True, slots=True)
class LoopBodyOwnership:
    """Canonical LOOP body ownership derived from immutable Plan config."""

    body_to_loop: Mapping[str, str]  # template_id → owning LOOP plan id
    loop_to_body: Mapping[str, tuple[str, ...]]  # LOOP plan id → body template ids


def build_loop_body_ownership(plan: ExecutionPlanV1) -> LoopBodyOwnership:
    """Derive body_template_id → owning_loop_plan_step_id (StaticComplexPlanValidator)."""
    id_set = {s.id for s in plan.steps}
    body_to_loop: dict[str, str] = {}
    loop_to_body: dict[str, tuple[str, ...]] = {}
    for step in plan.steps:
        if step.type != AuthorableStepType.LOOP:
            continue
        try:
            cfg = LoopStepConfigV1.model_validate(step.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Invalid LOOP config for {step.id!r}.",
                status_code=409,
            ) from exc
        bodies: list[str] = []
        for body_id in cfg.body_step_ids:
            if body_id not in id_set:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"LOOP {step.id!r} body_step_id={body_id!r} does not exist."
                    ),
                    status_code=409,
                )
            if body_id == step.id:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"LOOP {step.id!r} cannot own itself.",
                    status_code=409,
                )
            if body_id in body_to_loop and body_to_loop[body_id] != step.id:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"Step {body_id!r} belongs to multiple LOOP bodies.",
                    status_code=409,
                )
            body_to_loop[body_id] = step.id
            bodies.append(body_id)
        loop_to_body[step.id] = tuple(bodies)
    return LoopBodyOwnership(body_to_loop=body_to_loop, loop_to_body=loop_to_body)


def top_level_plan_steps(plan: ExecutionPlanV1) -> list[ExecutionPlanStep]:
    """Plan Steps that are not owned by any LOOP body (initial materialization)."""
    ownership = build_loop_body_ownership(plan)
    return [s for s in plan.steps if s.id not in ownership.body_to_loop]


def is_body_template(plan: ExecutionPlanV1, step_id: str) -> bool:
    return step_id in build_loop_body_ownership(plan).body_to_loop


def assert_flat_foreach_runtime_compatible(plan: ExecutionPlanV1) -> LoopBodyOwnership:
    """Fail closed before LOOP body MCP for WHILE / nested LOOP / body APPROVAL."""
    ownership = build_loop_body_ownership(plan)
    by_id = {s.id: s for s in plan.steps}
    for loop_id, body_ids in ownership.loop_to_body.items():
        loop_step = by_id[loop_id]
        cfg = LoopStepConfigV1.model_validate(loop_step.config)
        if cfg.mode != LoopMode.FOR_EACH:
            raise AppError(
                code=LOOP_RUNTIME_UNSUPPORTED,
                message=(
                    f"LOOP {loop_id!r} mode={cfg.mode.value!r} is unsupported "
                    "(FOR_EACH only in this runtime slice)."
                ),
                status_code=409,
            )
        # Nested LOOP: a body template that is itself a LOOP, or a body that
        # owns further bodies via being a LOOP id in body_to_loop values chain.
        if loop_id in ownership.body_to_loop:
            raise AppError(
                code=LOOP_RUNTIME_UNSUPPORTED,
                message=f"Nested LOOP {loop_id!r} runtime is unsupported.",
                status_code=409,
            )
        for body_id in body_ids:
            body = by_id[body_id]
            if body.type == AuthorableStepType.LOOP:
                raise AppError(
                    code=LOOP_RUNTIME_UNSUPPORTED,
                    message=(
                        f"Nested LOOP body template {body_id!r} runtime is "
                        "unsupported."
                    ),
                    status_code=409,
                )
            if body.type == AuthorableStepType.APPROVAL:
                raise AppError(
                    code=LOOP_RUNTIME_UNSUPPORTED,
                    message=(
                        f"APPROVAL inside LOOP body {body_id!r} is unsupported."
                    ),
                    status_code=409,
                )
            if body.type not in {
                AuthorableStepType.TOOL,
                AuthorableStepType.CONDITION,
                AuthorableStepType.JOIN,
            }:
                raise AppError(
                    code=LOOP_RUNTIME_UNSUPPORTED,
                    message=(
                        f"LOOP body step type {body.type.value!r} for "
                        f"{body_id!r} is unsupported."
                    ),
                    status_code=409,
                )
    return ownership


def iteration_step_key(
    *,
    parent_step_id: uuid.UUID,
    iteration_no: int,
    template_step_id: str,
) -> str:
    """Deterministic unique ExecutionStep.step_key for a body instance."""
    if iteration_no < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="iteration_no must be >= 1 for LOOP body instances.",
            status_code=409,
        )
    digest = hashlib.sha256(template_step_id.encode("utf-8")).hexdigest()
    return (
        f"loop:{parent_step_id.hex}:{iteration_no:06d}:{digest}"
    )


def template_id_from_step_snapshot(step: ExecutionStep) -> str:
    try:
        parsed = ExecutionPlanStep.model_validate(step.step_snapshot)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ExecutionStep step_snapshot is invalid for template identity.",
            status_code=409,
        ) from exc
    return parsed.id


def hash_collection(collection: Sequence[Any]) -> str:
    return compute_canonical_json_hash(list(collection))


def build_loop_context_projection(
    *,
    loop_plan_step_id: str,
    mode: str,
    iteration_no: int,
    item: Any,
    collection_size: int,
) -> dict[str, Any]:
    if iteration_no < 1:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="LOOP_CONTEXT iteration_no must be >= 1.",
            status_code=409,
        )
    return {
        "loop_step_id": loop_plan_step_id,
        "mode": mode,
        "iteration_no": iteration_no,
        "index": iteration_no - 1,
        "item": item,
        "collection_size": collection_size,
    }


def projected_expanded_step_count(
    *,
    top_level_count: int,
    collection_size: int,
    body_step_count: int,
) -> int:
    return top_level_count + (collection_size * body_step_count)


def tool_version_for_template(plan_step: ExecutionPlanStep) -> uuid.UUID | None:
    if plan_step.type != AuthorableStepType.TOOL:
        return None
    cfg = ComplexToolStepConfigV1.model_validate(plan_step.config)
    return cfg.tool_version_id


def child_instances_for_iteration(
    *,
    steps: Sequence[ExecutionStep],
    parent_step_id: uuid.UUID,
    iteration_no: int,
) -> list[ExecutionStep]:
    return [
        s
        for s in steps
        if s.parent_step_id == parent_step_id and s.iteration_no == iteration_no
    ]


def max_materialized_iteration(
    *,
    steps: Sequence[ExecutionStep],
    parent_step_id: uuid.UUID,
) -> int:
    values = [
        s.iteration_no
        for s in steps
        if s.parent_step_id == parent_step_id and s.iteration_no is not None
    ]
    return max(values) if values else 0
