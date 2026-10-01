"""Flat FOR_EACH / WHILE LOOP runtime helpers (docs/04 §9.5).

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
    """Fail closed before LOOP body MCP for nested LOOP / body APPROVAL.

    Flat FOR_EACH and WHILE are supported. Nested LOOP and body APPROVAL remain
    unsupported (``LOOP_RUNTIME_UNSUPPORTED``).
    """
    ownership = build_loop_body_ownership(plan)
    by_id = {s.id: s for s in plan.steps}
    for loop_id, body_ids in ownership.loop_to_body.items():
        loop_step = by_id[loop_id]
        cfg = LoopStepConfigV1.model_validate(loop_step.config)
        if cfg.mode not in {LoopMode.FOR_EACH, LoopMode.WHILE}:
            raise AppError(
                code=LOOP_RUNTIME_UNSUPPORTED,
                message=(
                    f"LOOP {loop_id!r} mode={cfg.mode.value!r} is unsupported."
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
    """FOR_EACH LOOP_CONTEXT projection (unchanged from #50)."""
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


def build_while_loop_context_projection(
    *,
    loop_plan_step_id: str,
    iteration_no: int,
    max_iterations: int,
    previous_iteration: dict[str, Any] | None,
) -> dict[str, Any]:
    """WHILE LOOP_CONTEXT projection for candidate/current iteration N."""
    if iteration_no < 1:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="WHILE LOOP_CONTEXT iteration_no must be >= 1.",
            status_code=409,
        )
    if max_iterations < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WHILE max_iterations must be >= 1.",
            status_code=409,
        )
    return {
        "loop_step_id": loop_plan_step_id,
        "mode": LoopMode.WHILE.value,
        "iteration_no": iteration_no,
        "index": iteration_no - 1,
        "max_iterations": max_iterations,
        "previous_iteration": previous_iteration,
    }


def previous_iteration_step_projection(step: ExecutionStep) -> dict[str, Any]:
    """Exact durable previous-iteration Step projection for WHILE LOOP_CONTEXT."""
    return {
        "status": step.status,
        "condition_result": step.condition_result,
        "error_code": step.error_code,
        "result_inline": step.result_inline,
    }


def _assert_sha256_hex(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"{field} must be a 64-char lowercase sha256 hex.",
            status_code=409,
        )
    return value


def parse_while_predicate_history(resolved_input: Any) -> dict[str, Any]:
    """Validate WHILE LOOP.resolved_input exact control evidence shape."""
    if not isinstance(resolved_input, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP resolved_input must be a WHILE control object.",
            status_code=409,
        )
    if set(resolved_input.keys()) != {"mode", "predicate_history"}:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP resolved_input has unexpected WHILE control fields.",
            status_code=409,
        )
    mode = resolved_input.get("mode")
    if mode != LoopMode.WHILE.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"LOOP resolved_input mode must be WHILE (got {mode!r}).",
            status_code=409,
        )
    history = resolved_input.get("predicate_history")
    if not isinstance(history, list):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WHILE predicate_history must be a list.",
            status_code=409,
        )
    parsed_entries: list[dict[str, Any]] = []
    for idx, entry in enumerate(history):
        if not isinstance(entry, dict):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WHILE predicate_history entry must be an object.",
                status_code=409,
            )
        if set(entry.keys()) != {
            "next_iteration_no",
            "evidence_hash",
            "result",
        }:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WHILE predicate_history entry has unexpected fields.",
                status_code=409,
            )
        n = entry.get("next_iteration_no")
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WHILE next_iteration_no must be an int >= 1.",
                status_code=409,
            )
        if n != idx + 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "WHILE predicate_history next_iteration_no values must be "
                    "contiguous starting at 1."
                ),
                status_code=409,
            )
        digest = _assert_sha256_hex(
            entry.get("evidence_hash"), field="evidence_hash"
        )
        result = entry.get("result")
        if not isinstance(result, bool):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WHILE predicate_history result must be a bool.",
                status_code=409,
            )
        parsed_entries.append(
            {
                "next_iteration_no": n,
                "evidence_hash": digest,
                "result": result,
            }
        )
    return {
        "mode": mode,
        "predicate_history": parsed_entries,
    }


def empty_while_control_evidence() -> dict[str, Any]:
    return {"mode": LoopMode.WHILE.value, "predicate_history": []}


def history_entry_for(
    history: Sequence[Mapping[str, Any]], *, next_iteration_no: int
) -> Mapping[str, Any] | None:
    for entry in history:
        if entry.get("next_iteration_no") == next_iteration_no:
            return entry
    return None


def result_inline_hash(result_inline: Any) -> str:
    """Canonical hash of durable result_inline (null-safe)."""
    return compute_canonical_json_hash(result_inline)


def build_previous_iteration_projection(
    *,
    steps: Sequence[ExecutionStep],
    parent_step_id: uuid.UUID,
    previous_iteration_no: int,
    body_step_ids: Sequence[str],
) -> dict[str, Any]:
    """Build complete previous_iteration projection for candidate N > 1.

    Requires exact body template set, all children terminal, immutable lineage.
    """
    if previous_iteration_no < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="previous_iteration_no must be >= 1.",
            status_code=409,
        )
    children = child_instances_for_iteration(
        steps=steps,
        parent_step_id=parent_step_id,
        iteration_no=previous_iteration_no,
    )
    if len(children) != len(body_step_ids):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"WHILE previous iteration {previous_iteration_no} has "
                f"{len(children)} children; expected {len(body_step_ids)}."
            ),
            status_code=409,
        )
    by_template: dict[str, ExecutionStep] = {}
    for child in children:
        tid = template_id_from_step_snapshot(child)
        if tid in by_template:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE previous iteration {previous_iteration_no} has "
                    f"duplicate template {tid!r}."
                ),
                status_code=409,
            )
        by_template[tid] = child
    if set(by_template) != set(body_step_ids):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"WHILE previous iteration {previous_iteration_no} template set "
                f"{sorted(by_template)} != body_step_ids {list(body_step_ids)}."
            ),
            status_code=409,
        )
    _ITERATION_TERMINAL = frozenset(
        {
            "SUCCEEDED",
            "FAILED",
            "TIMED_OUT",
            "SKIPPED",
            "CANCELLED",
            "UNKNOWN_OUTCOME",
        }
    )
    step_map: dict[str, dict[str, Any]] = {}
    for tid in body_step_ids:
        child = by_template[tid]
        if child.status not in _ITERATION_TERMINAL:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE previous iteration {previous_iteration_no} step "
                    f"{tid!r} is nonterminal ({child.status!r})."
                ),
                status_code=409,
            )
        expected_key = iteration_step_key(
            parent_step_id=parent_step_id,
            iteration_no=previous_iteration_no,
            template_step_id=tid,
        )
        if child.step_key != expected_key:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE previous iteration child key drift for {tid!r}."
                ),
                status_code=409,
            )
        step_map[tid] = previous_iteration_step_projection(child)
    return {
        "iteration_no": previous_iteration_no,
        "steps": step_map,
    }


def hash_while_predicate_evidence(evidence: Mapping[str, Any]) -> str:
    return compute_canonical_json_hash(dict(evidence))


def append_or_replay_while_history(
    *,
    resolved_input: dict[str, Any],
    next_iteration_no: int,
    evidence_hash: str,
    result: bool,
) -> dict[str, Any]:
    """Append or replay-validate one WHILE predicate_history entry.

    Returns the updated control evidence object.
    """
    control = parse_while_predicate_history(resolved_input)
    history = list(control["predicate_history"])
    digest = _assert_sha256_hex(evidence_hash, field="evidence_hash")
    if not isinstance(result, bool):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WHILE predicate result must be a bool.",
            status_code=409,
        )
    existing = history_entry_for(history, next_iteration_no=next_iteration_no)
    if existing is None:
        if next_iteration_no != len(history) + 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE history append for candidate {next_iteration_no} "
                    f"but history length is {len(history)}."
                ),
                status_code=409,
            )
        history.append(
            {
                "next_iteration_no": next_iteration_no,
                "evidence_hash": digest,
                "result": result,
            }
        )
    else:
        if (
            existing["evidence_hash"] != digest
            or existing["result"] is not result
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE predicate_history replay mismatch for candidate "
                    f"{next_iteration_no}."
                ),
                status_code=409,
            )
    updated = {"mode": LoopMode.WHILE.value, "predicate_history": history}
    return parse_while_predicate_history(updated)


def projected_expanded_step_count(
    *,
    top_level_count: int,
    collection_size: int,
    body_step_count: int,
) -> int:
    """Legacy single-LOOP projection (kept for unit helpers / docs examples)."""
    return top_level_count + (collection_size * body_step_count)


def parse_foreach_collection_pin(resolved_input: Any) -> dict[str, Any]:
    """Validate LOOP.resolved_input safe control evidence shape.

    Exact keys: mode / collection_hash / collection_size. Rejects bool-as-int
    and unexpected fields.
    """
    if not isinstance(resolved_input, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP resolved_input must be a FOR_EACH control object.",
            status_code=409,
        )
    if set(resolved_input.keys()) != {
        "mode",
        "collection_hash",
        "collection_size",
    }:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP resolved_input has unexpected FOR_EACH control fields.",
            status_code=409,
        )
    mode = resolved_input.get("mode")
    if mode != LoopMode.FOR_EACH.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"LOOP resolved_input mode must be FOR_EACH (got {mode!r}).",
            status_code=409,
        )
    digest = resolved_input.get("collection_hash")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP collection_hash must be a 64-char lowercase sha256 hex.",
            status_code=409,
        )
    size = resolved_input.get("collection_size")
    # bool is a subclass of int — reject explicitly.
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP collection_size must be an int >= 0 (bool rejected).",
            status_code=409,
        )
    return {
        "mode": mode,
        "collection_hash": digest,
        "collection_size": size,
    }


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
