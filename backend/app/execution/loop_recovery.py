"""Expired RUNNING flat FOR_EACH / WHILE LOOP recovery validation (Issue #117).

Recovery validates durable LOOP checkpoints before takeover. Progression after a
fresh lease reuses ExecutionOrchestrator / loop_reconcile — this module does not
materialize iterations, append predicate_history, or invoke MCP.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, LoopMode, StepStatus
from app.execution.loop_reconcile import (
    build_while_evidence_object,
    revalidate_foreach_collection_pin,
    runtime_plan_step,
)
from app.execution.loop_runtime import (
    LOOP_RUNTIME_UNSUPPORTED,
    LoopBodyOwnership,
    assert_flat_foreach_runtime_compatible,
    assert_while_child_immutable_lineage,
    child_instances_for_iteration,
    hash_while_predicate_evidence,
    history_entry_for,
    iteration_step_key,
    max_materialized_iteration,
    parse_foreach_collection_pin,
    parse_while_predicate_history,
    template_id_from_step_snapshot,
    tool_version_for_template,
    top_level_plan_steps,
)
from app.models.execution import Execution, ExecutionStep
from app.schemas.execution_plan import (
    ExecutionPlanStep,
    ExecutionPlanV1,
    LoopStepConfigV1,
)

_STEP_TERMINAL = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.CANCELLED.value,
        StepStatus.SKIPPED.value,
        StepStatus.UNKNOWN_OUTCOME.value,
    }
)

_ACTIVE_NONTERMINAL = frozenset(
    {
        StepStatus.PENDING.value,
        StepStatus.READY.value,
        StepStatus.RUNNING.value,
        StepStatus.WAITING_INPUT.value,
        StepStatus.WAITING_APPROVAL.value,
    }
)


def plan_supports_flat_loop_running_recovery(plan: ExecutionPlanV1) -> bool:
    """True when Plan LOOPs are runtime-supported flat FOR_EACH / WHILE only."""
    if not any(s.type == AuthorableStepType.LOOP for s in plan.steps):
        return False
    try:
        assert_flat_foreach_runtime_compatible(plan)
    except AppError:
        return False
    return True


def assert_loop_recovery_ownership(plan: ExecutionPlanV1) -> LoopBodyOwnership:
    """Fail closed unless Plan is flat FOR_EACH/WHILE recovery-compatible."""
    try:
        return assert_flat_foreach_runtime_compatible(plan)
    except AppError as exc:
        if exc.code == LOOP_RUNTIME_UNSUPPORTED:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=exc.message,
                status_code=409,
            ) from exc
        raise


def assert_execution_loop_recovery_checkpoints(
    *,
    execution: Execution,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
    ownership: LoopBodyOwnership,
) -> None:
    """Prove LOOP parents, control evidence, and every child before takeover."""
    plan_by_id = {s.id: s for s in plan.steps}
    top_expected = {s.id for s in top_level_plan_steps(plan)}
    body_templates = set(ownership.body_to_loop)

    top_rows = [s for s in steps if s.parent_step_id is None]
    child_rows = [s for s in steps if s.parent_step_id is not None]

    # No top-level runtime row for a body template.
    for row in top_rows:
        if row.step_key in body_templates:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Body template {row.step_key!r} must not exist as a "
                    "top-level ExecutionStep."
                ),
                status_code=409,
            )
        if row.step_key not in top_expected:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Unexpected top-level Step {row.step_key!r} for LOOP "
                    "recovery."
                ),
                status_code=409,
            )

    # Required top-level LOOP/non-LOOP rows must be present.
    present_top = {s.step_key for s in top_rows}
    missing = top_expected - present_top
    if missing:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"Missing top-level Steps for LOOP recovery: "
                f"{sorted(missing)}."
            ),
            status_code=409,
        )

    parent_by_id = {s.id: s for s in top_rows}
    for child in child_rows:
        parent = parent_by_id.get(child.parent_step_id)  # type: ignore[arg-type]
        if parent is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP child references missing parent LOOP Step.",
                status_code=409,
            )
        if parent.step_type != AuthorableStepType.LOOP.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP child parent must be a LOOP Step.",
                status_code=409,
            )

    for top in top_rows:
        if top.step_type != AuthorableStepType.LOOP.value:
            continue
        plan_step = plan_by_id[top.step_key]
        try:
            cfg = LoopStepConfigV1.model_validate(plan_step.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"LOOP {top.step_key!r} config is invalid.",
                status_code=409,
            ) from exc
        if cfg.mode == LoopMode.FOR_EACH:
            _assert_foreach_recovery_checkpoint(
                execution=execution,
                plan=plan,
                steps=steps,
                parent=top,
                cfg=cfg,
                plan_step=plan_step,
            )
        elif cfg.mode == LoopMode.WHILE:
            _assert_while_recovery_checkpoint(
                execution=execution,
                plan=plan,
                steps=steps,
                parent=top,
                cfg=cfg,
                plan_step=plan_step,
            )
        else:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"LOOP {top.step_key!r} mode {cfg.mode.value!r} is "
                    "unsupported for RUNNING recovery."
                ),
                status_code=409,
            )


def _assert_parent_plan_projection(
    *, parent: ExecutionStep, plan_step: ExecutionPlanStep
) -> None:
    if parent.step_key != plan_step.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP parent step_key does not match Plan LOOP id.",
            status_code=409,
        )
    if plan_step.model_dump(mode="json") != parent.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} parent snapshot is not an exact "
                "Plan Step projection."
            ),
            status_code=409,
        )


def _assert_iteration_child_set(
    *,
    parent: ExecutionStep,
    steps: Sequence[ExecutionStep],
    cfg: LoopStepConfigV1,
    plan: ExecutionPlanV1,
    iteration_no: int,
    require_terminal: bool,
) -> list[ExecutionStep]:
    children = child_instances_for_iteration(
        steps=steps, parent_step_id=parent.id, iteration_no=iteration_no
    )
    body_ids = tuple(cfg.body_step_ids)
    if not children:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} iteration {iteration_no} has no "
                "child rows (partial materialization is corruption)."
            ),
            status_code=409,
        )
    if len(children) != len(body_ids):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} iteration {iteration_no} is a "
                "partial materialization."
            ),
            status_code=409,
        )
    templates: list[str] = []
    for child in children:
        _assert_dynamic_child_lineage(
            parent=parent, child=child, cfg=cfg, plan=plan
        )
        templates.append(template_id_from_step_snapshot(child))
    if len(templates) != len(set(templates)):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} iteration {iteration_no} has "
                "duplicate body templates."
            ),
            status_code=409,
        )
    if set(templates) != set(body_ids):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} iteration {iteration_no} template "
                f"set {sorted(templates)} != body_step_ids {list(body_ids)}."
            ),
            status_code=409,
        )
    if require_terminal:
        for child in children:
            if child.status not in _STEP_TERMINAL:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"LOOP {parent.step_key!r} iteration {iteration_no} "
                        "is not terminal while a newer iteration exists."
                    ),
                    status_code=409,
                )
            if child.status == StepStatus.UNKNOWN_OUTCOME.value:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"Durable UNKNOWN_OUTCOME under LOOP "
                        f"{parent.step_key!r} iteration {iteration_no} "
                        "blocks clean recovery resume."
                    ),
                    status_code=409,
                )
    # Active highest iteration: leave UNKNOWN_OUTCOME to TOOL classification
    # (FATAL_UNKNOWN → Execution FAILED / MCP 0). Do not convert to READY.
    return children


def _assert_dynamic_child_lineage(
    *,
    parent: ExecutionStep,
    child: ExecutionStep,
    cfg: LoopStepConfigV1,
    plan: ExecutionPlanV1,
) -> None:
    if child.parent_step_id != parent.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child parent_step_id mismatch.",
            status_code=409,
        )
    if child.iteration_no is None or child.iteration_no < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child iteration_no must be >= 1.",
            status_code=409,
        )
    try:
        template = ExecutionPlanStep.model_validate(child.step_snapshot)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child step_snapshot is invalid.",
            status_code=409,
        ) from exc
    expected = next((ps for ps in plan.steps if ps.id == template.id), None)
    if expected is None or expected.model_dump(mode="json") != child.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP child snapshot is not an exact body template "
                f"projection for {template.id!r}."
            ),
            status_code=409,
        )
    if template.id not in cfg.body_step_ids:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP child template {template.id!r} is not owned by "
                f"LOOP {parent.step_key!r}."
            ),
            status_code=409,
        )
    if child.step_type != template.type.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child step_type mismatches body template.",
            status_code=409,
        )
    expected_key = iteration_step_key(
        parent_step_id=parent.id,
        iteration_no=child.iteration_no,
        template_step_id=template.id,
    )
    if child.step_key != expected_key:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP child step_key {child.step_key!r} does not match "
                f"deterministic key {expected_key!r}."
            ),
            status_code=409,
        )
    expected_tv = tool_version_for_template(expected)
    if child.mcp_tool_version_id != expected_tv:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="LOOP child TOOL mcp_tool_version_id mismatches template.",
            status_code=409,
        )
    if cfg.mode == LoopMode.WHILE:
        assert_while_child_immutable_lineage(
            child=child,
            parent_step_id=parent.id,
            iteration_no=child.iteration_no,
            template=expected,
        )


def _assert_iteration_topology(
    *,
    parent: ExecutionStep,
    steps: Sequence[ExecutionStep],
    cfg: LoopStepConfigV1,
    plan: ExecutionPlanV1,
    max_bound: int,
) -> int:
    children = [s for s in steps if s.parent_step_id == parent.id]
    max_n = max_materialized_iteration(steps=children, parent_step_id=parent.id)
    if max_n < 1:
        if children:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"LOOP {parent.step_key!r} has child rows without a "
                    "valid iteration_no."
                ),
                status_code=409,
            )
        return 0
    if max_n > max_bound:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} materialized iteration count "
                f"{max_n} exceeds bound {max_bound}."
            ),
            status_code=409,
        )
    present = {c.iteration_no for c in children if c.iteration_no is not None}
    if present != set(range(1, max_n + 1)):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} materialized iterations "
                f"{sorted(present)} are not contiguous 1..{max_n}."
            ),
            status_code=409,
        )

    active_iters: list[int] = []
    for n in range(1, max_n + 1):
        iter_children = _assert_iteration_child_set(
            parent=parent,
            steps=steps,
            cfg=cfg,
            plan=plan,
            iteration_no=n,
            require_terminal=(n < max_n),
        )
        if any(c.status in _ACTIVE_NONTERMINAL for c in iter_children):
            active_iters.append(n)
        elif n == max_n and any(
            c.status == StepStatus.UNKNOWN_OUTCOME.value for c in iter_children
        ):
            # Already raised in _assert_iteration_child_set for UNKNOWN.
            pass

    if len(active_iters) > 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} has multiple active iterations "
                f"{active_iters}."
            ),
            status_code=409,
        )
    if active_iters and active_iters[0] != max_n:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"LOOP {parent.step_key!r} active iteration "
                f"{active_iters[0]} is not the highest materialized "
                f"iteration {max_n}."
            ),
            status_code=409,
        )

    if parent.status in _STEP_TERMINAL and active_iters:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"Terminal LOOP {parent.step_key!r} still has active child "
                "evidence."
            ),
            status_code=409,
        )
    return max_n


def _assert_foreach_recovery_checkpoint(
    *,
    execution: Execution,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
    parent: ExecutionStep,
    cfg: LoopStepConfigV1,
    plan_step: ExecutionPlanStep,
) -> None:
    _assert_parent_plan_projection(parent=parent, plan_step=plan_step)
    if parent.status != StepStatus.RUNNING.value:
        # Terminal / PENDING parents: still validate children if present.
        if parent.status in _STEP_TERMINAL:
            children = [s for s in steps if s.parent_step_id == parent.id]
            if children:
                pin = parse_foreach_collection_pin(parent.resolved_input)
                _assert_iteration_topology(
                    parent=parent,
                    steps=steps,
                    cfg=cfg,
                    plan=plan,
                    max_bound=pin["collection_size"],
                )
            return
        return

    # RUNNING FOR_EACH requires a valid collection pin (not a fresh re-pin).
    if parent.resolved_input is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"RUNNING FOR_EACH {parent.step_key!r} missing collection pin."
            ),
            status_code=409,
        )
    pin = parse_foreach_collection_pin(parent.resolved_input)
    collection = revalidate_foreach_collection_pin(
        execution=execution,
        loop_step=parent,
        plan=plan,
        steps=steps,
    )
    if pin["collection_size"] != len(collection):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="FOR_EACH collection_size drifted from re-resolved collection.",
            status_code=409,
        )
    if pin["collection_size"] > cfg.max_iterations:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"FOR_EACH {parent.step_key!r} pinned collection_size "
                f"{pin['collection_size']} exceeds max_iterations="
                f"{cfg.max_iterations}."
            ),
            status_code=409,
        )
    max_n = _assert_iteration_topology(
        parent=parent,
        steps=steps,
        cfg=cfg,
        plan=plan,
        max_bound=pin["collection_size"],
    )
    if max_n > pin["collection_size"]:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"FOR_EACH {parent.step_key!r} materialized count exceeds "
                "pinned collection_size."
            ),
            status_code=409,
        )


def _revalidate_while_history_entry_hash(
    *,
    execution: Execution,
    parent: ExecutionStep,
    plan: ExecutionPlanV1,
    cfg: LoopStepConfigV1,
    steps: Sequence[ExecutionStep],
    next_iteration_no: int,
    entry: Mapping[str, Any],
) -> None:
    evidence = build_while_evidence_object(
        plan=plan,
        execution=execution,
        loop_step=parent,
        cfg=cfg,
        steps=steps,
        candidate_iteration_no=next_iteration_no,
    )
    rebuilt = hash_while_predicate_evidence(evidence)
    if rebuilt != entry["evidence_hash"]:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"WHILE {parent.step_key!r} evidence_hash drift for candidate "
                f"{next_iteration_no}."
            ),
            status_code=409,
        )


def _assert_while_recovery_checkpoint(
    *,
    execution: Execution,
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
    parent: ExecutionStep,
    cfg: LoopStepConfigV1,
    plan_step: ExecutionPlanStep,
) -> None:
    _assert_parent_plan_projection(parent=parent, plan_step=plan_step)
    if parent.status != StepStatus.RUNNING.value:
        if parent.status in _STEP_TERMINAL and parent.resolved_input is not None:
            # Defer full terminal invariants to DAG validation after takeover;
            # still reject active children under terminal parent.
            children = [s for s in steps if s.parent_step_id == parent.id]
            if children and any(c.status in _ACTIVE_NONTERMINAL for c in children):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"Terminal WHILE {parent.step_key!r} still has active "
                        "child evidence."
                    ),
                    status_code=409,
                )
        return

    if parent.resolved_input is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"RUNNING WHILE {parent.step_key!r} missing predicate_history "
                "control evidence."
            ),
            status_code=409,
        )
    control = parse_while_predicate_history(parent.resolved_input)
    history = control["predicate_history"]
    children = [s for s in steps if s.parent_step_id == parent.id]
    max_n = max_materialized_iteration(steps=children, parent_step_id=parent.id)

    if max_n < 1:
        # Checkpoints: empty history, or history [1=true] before materialize.
        if not history:
            return
        if len(history) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE {parent.step_key!r} RUNNING with no children has "
                    "non-singleton predicate_history."
                ),
                status_code=409,
            )
        entry = history_entry_for(history, next_iteration_no=1)
        if entry is None or entry["result"] is not True:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE {parent.step_key!r} RUNNING with no children "
                    "requires history gate 1=true (or empty history)."
                ),
                status_code=409,
            )
        _revalidate_while_history_entry_hash(
            execution=execution,
            parent=parent,
            plan=plan,
            cfg=cfg,
            steps=steps,
            next_iteration_no=1,
            entry=entry,
        )
        return

    max_n = _assert_iteration_topology(
        parent=parent,
        steps=steps,
        cfg=cfg,
        plan=plan,
        max_bound=cfg.max_iterations,
    )

    # Every materialized iteration requires a true gate; revalidate hashes.
    for n in range(1, max_n + 1):
        entry = history_entry_for(history, next_iteration_no=n)
        if entry is None or entry["result"] is not True:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE {parent.step_key!r} materialized iteration {n} "
                    "lacks a true predicate_history gate."
                ),
                status_code=409,
            )
        _revalidate_while_history_entry_hash(
            execution=execution,
            parent=parent,
            plan=plan,
            cfg=cfg,
            steps=steps,
            next_iteration_no=n,
            entry=entry,
        )

    # No false gate at or before N; no rows after a false gate.
    for entry in history:
        n = int(entry["next_iteration_no"])
        if n <= max_n and entry["result"] is False:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE {parent.step_key!r} has false gate at or before "
                    f"materialized iteration {max_n}."
                ),
                status_code=409,
            )
        if entry["result"] is False and any(
            c.iteration_no is not None and c.iteration_no >= n for c in children
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE {parent.step_key!r} has child rows after false "
                    f"gate candidate {n}."
                ),
                status_code=409,
            )

    nxt = history_entry_for(history, next_iteration_no=max_n + 1)
    if nxt is None:
        # Checkpoint B: iters complete or active; no next gate yet.
        if len(history) != max_n:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"WHILE {parent.step_key!r} predicate_history length "
                    f"{len(history)} is inconsistent with materialized "
                    f"iterations {max_n}."
                ),
                status_code=409,
            )
        return

    # Checkpoint C (true) / D (false): next gate persisted; no N+1 rows yet
    # when true, or parent still RUNNING when false.
    if len(history) != max_n + 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"WHILE {parent.step_key!r} predicate_history extends past "
                f"candidate {max_n + 1}."
            ),
            status_code=409,
        )
    # Prior iterations must be complete/terminal before a next-gate-only checkpoint.
    active = [
        c
        for c in children
        if c.iteration_no == max_n and c.status in _ACTIVE_NONTERMINAL
    ]
    if active and nxt["result"] is True:
        # Active highest iteration with gate N=true is checkpoint E — next gate
        # must not exist yet.
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"WHILE {parent.step_key!r} has active iteration {max_n} and "
                f"also a persisted gate for candidate {max_n + 1}."
            ),
            status_code=409,
        )
    if active and nxt["result"] is False:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"WHILE {parent.step_key!r} has active iteration {max_n} and "
                f"a false gate for candidate {max_n + 1}."
            ),
            status_code=409,
        )
    # Complete 1..N + gate N+1 without N+1 rows.
    if any(c.iteration_no == max_n + 1 for c in children):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"WHILE {parent.step_key!r} has unexpected rows for "
                f"iteration {max_n + 1}."
            ),
            status_code=409,
        )
    _revalidate_while_history_entry_hash(
        execution=execution,
        parent=parent,
        plan=plan,
        cfg=cfg,
        steps=steps,
        next_iteration_no=max_n + 1,
        entry=nxt,
    )


def body_template_plan_step_id(step: ExecutionStep, plan_by_id: Mapping[str, Any]) -> str:
    """Policy / lineage Plan step id for a runtime Step (top-level or LOOP child)."""
    return runtime_plan_step(step, plan_by_id).id
