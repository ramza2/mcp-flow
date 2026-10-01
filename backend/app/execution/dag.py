"""TOOL/CONDITION/JOIN/APPROVAL/LOOP DAG runtime validation and local evaluation.

Wave scheduling lives in ``ExecutionOrchestrator``; this module is pure graph
logic (no DB writes except callers applying returned decisions).

Flat FOR_EACH LOOP is supported: body templates are not initial ExecutionSteps;
iteration instances use deterministic child step_keys. WHILE / nested LOOP /
body APPROVAL fail closed via ``assert_flat_foreach_runtime_compatible``.
Authorable APPROVAL is a Plan checkpoint (distinct from ToolPolicy approval).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, JoinPolicy, StepStatus
from app.execution.completion import is_continuable_known_failure, plan_step_for
from app.execution.loop_runtime import (
    assert_flat_foreach_runtime_compatible,
    build_loop_body_ownership,
    iteration_step_key,
    template_id_from_step_snapshot,
    tool_version_for_template,
    top_level_plan_steps,
)
from app.models.execution import ExecutionStep
from app.schemas.execution_plan import (
    ApprovalStepConfigV1,
    ComplexToolStepConfigV1,
    ConditionStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    JoinStepConfigV1,
    LoopStepConfigV1,
)

_JOIN_BARRIER_TERMINAL = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.CANCELLED.value,
        StepStatus.SKIPPED.value,
        StepStatus.UNKNOWN_OUTCOME.value,
    }
)

_TOP_LEVEL_RUNTIME_TYPES = frozenset(
    {
        AuthorableStepType.TOOL.value,
        AuthorableStepType.CONDITION.value,
        AuthorableStepType.JOIN.value,
        AuthorableStepType.APPROVAL.value,
        AuthorableStepType.LOOP.value,
    }
)

_BODY_RUNTIME_TYPES = frozenset(
    {
        AuthorableStepType.TOOL.value,
        AuthorableStepType.CONDITION.value,
        AuthorableStepType.JOIN.value,
    }
)

# Intentional conditional control-flow skips (not fail-fast cleanup).
INTENTIONAL_CONDITIONAL_SKIP_CODES = frozenset(
    {
        "STEP_WHEN_FALSE",
        "UPSTREAM_CONDITION_SKIPPED",
    }
)


@dataclass(frozen=True, slots=True)
class ToolJoinDag:
    """Validated runtime DAG (top-level + active LOOP iteration instances)."""

    ordered_step_keys: tuple[str, ...]
    root_tool_keys: tuple[str, ...]
    max_parallelism: int
    dependents: Mapping[str, tuple[str, ...]]
    dependencies: Mapping[str, tuple[str, ...]]


def _validate_typed_config(
    *,
    ps: ExecutionPlanStep,
    step: ExecutionStep,
    depends_on: list[str],
    allow_loop: bool,
) -> None:
    if ps.type == AuthorableStepType.TOOL:
        if step.mcp_tool_version_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"TOOL Step {ps.id!r} missing mcp_tool_version_id.",
                status_code=409,
            )
        try:
            cfg = ComplexToolStepConfigV1.model_validate(ps.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Invalid TOOL config for {ps.id!r}.",
                status_code=409,
            ) from exc
        if cfg.tool_version_id != step.mcp_tool_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"TOOL {ps.id!r} mcp_tool_version_id does not match "
                    "config.tool_version_id."
                ),
                status_code=409,
            )
        # Direct TOOL fan-in unsupported — require explicit JOIN.
        # Body entry may depend on LOOP + ignore that for fan-in count of body deps.
        body_fan_in = [d for d in depends_on]
        if len(body_fan_in) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"TOOL {ps.id!r} direct fan-in is unsupported; "
                    "use an explicit JOIN."
                ),
                status_code=409,
            )
        return
    if ps.type == AuthorableStepType.CONDITION:
        if step.mcp_tool_version_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"CONDITION Step {ps.id!r} must have null mcp_tool_version_id."
                ),
                status_code=409,
            )
        try:
            ConditionStepConfigV1.model_validate(ps.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Invalid CONDITION config for {ps.id!r}.",
                status_code=409,
            ) from exc
        return
    if ps.type == AuthorableStepType.JOIN:
        if step.mcp_tool_version_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"JOIN Step {ps.id!r} must have null mcp_tool_version_id.",
                status_code=409,
            )
        try:
            JoinStepConfigV1.model_validate(ps.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Invalid JOIN config for {ps.id!r}.",
                status_code=409,
            ) from exc
        if not depends_on:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"JOIN {ps.id!r} requires one or more dependencies.",
                status_code=409,
            )
        return
    if ps.type == AuthorableStepType.APPROVAL:
        if step.mcp_tool_version_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"APPROVAL Step {ps.id!r} must have null mcp_tool_version_id."
                ),
                status_code=409,
            )
        try:
            ApprovalStepConfigV1.model_validate(ps.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Invalid APPROVAL config for {ps.id!r}.",
                status_code=409,
            ) from exc
        return
    if allow_loop and ps.type == AuthorableStepType.LOOP:
        if step.mcp_tool_version_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"LOOP Step {ps.id!r} must have null mcp_tool_version_id.",
                status_code=409,
            )
        try:
            LoopStepConfigV1.model_validate(ps.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Invalid LOOP config for {ps.id!r}.",
                status_code=409,
            ) from exc
        return
    raise AppError(
        code="RESOURCE_CONFLICT",
        message=f"Unsupported Plan Step type {ps.type!r}.",
        status_code=409,
    )


def _assert_exact_plan_projection(
    *, ps: ExecutionPlanStep, step: ExecutionStep
) -> None:
    try:
        parsed = ExecutionPlanStep.model_validate(step.step_snapshot)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"step_snapshot invalid for {ps.id!r}.",
            status_code=409,
        ) from exc
    if parsed.id != ps.id or parsed.type != ps.type:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"Step identity/type mismatch for {ps.id!r}.",
            status_code=409,
        )
    if parsed.model_dump(mode="json") != step.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"step_snapshot drift for {ps.id!r}.",
            status_code=409,
        )
    if ps.model_dump(mode="json") != step.step_snapshot:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"step_snapshot is not an exact projection of immutable "
                f"Plan Step {ps.id!r}."
            ),
            status_code=409,
        )
    if step.step_type != ps.type.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                f"ExecutionStep.step_type {step.step_type!r} does not match "
                f"immutable Plan Step type {ps.type.value!r} for {ps.id!r}."
            ),
            status_code=409,
        )


def validate_tool_join_dag(
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
) -> ToolJoinDag:
    """Fail closed unless Steps form a valid runtime DAG.

    Without LOOP bodies: exact ``plan.steps`` ↔ ExecutionStep projection
    (historical TOOL/CONDITION/JOIN/APPROVAL contract).

    With LOOP: top-level rows match non-body Plan Steps (including LOOP);
    child iteration rows are validated separately and included in the
    scheduling graph. Body entry edges (depends_on owning LOOP) are satisfied
    while the parent LOOP is RUNNING (see eligibility helpers).
    """
    if not steps:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution has no Steps to orchestrate.",
            status_code=409,
        )

    by_key = {s.step_key: s for s in steps}
    if len(by_key) != len(steps):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ExecutionStep step_key values are not unique.",
            status_code=409,
        )

    ownership = build_loop_body_ownership(plan)
    has_loop_bodies = bool(ownership.body_to_loop)
    plan_by_id = {ps.id: ps for ps in plan.steps}

    if has_loop_bodies:
        assert_flat_foreach_runtime_compatible(plan)

    top_level_plan = top_level_plan_steps(plan)
    top_level_ids = {ps.id for ps in top_level_plan}
    top_rows = [s for s in steps if s.parent_step_id is None]
    child_rows = [s for s in steps if s.parent_step_id is not None]

    if not has_loop_bodies:
        if child_rows:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Runtime DAG Steps must have parent_step_id null.",
                status_code=409,
            )
        if len(plan.steps) != len(steps):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution Step count does not match plan.steps.",
                status_code=409,
            )
        if set(by_key) != {ps.id for ps in plan.steps}:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ExecutionStep keys do not exactly match Plan Step IDs.",
                status_code=409,
            )
        # Historical path: LOOP type without body ownership is impossible for
        # valid plans; reject LOOP when no body map (defensive).
        for s in steps:
            if s.step_type == AuthorableStepType.LOOP.value:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"Step type {s.step_type!r} unsupported without LOOP "
                        "body ownership."
                    ),
                    status_code=409,
                )
    else:
        if any(s.iteration_no is not None for s in top_rows):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Top-level runtime Steps must have iteration_no null.",
                status_code=409,
            )
        top_by_key = {s.step_key: s for s in top_rows}
        if set(top_by_key) != top_level_ids:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Top-level ExecutionStep keys do not exactly match "
                    "non-body Plan Step IDs."
                ),
                status_code=409,
            )

    deps: dict[str, tuple[str, ...]] = {}
    dependents: dict[str, list[str]] = {}

    # --- Top-level validation ---
    for ps in top_level_plan:
        step = by_key[ps.id]
        if step.parent_step_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Top-level Step {ps.id!r} has parent_step_id set.",
                status_code=409,
            )
        _assert_exact_plan_projection(ps=ps, step=step)
        if step.step_type not in _TOP_LEVEL_RUNTIME_TYPES:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Unsupported runtime step_type {step.step_type!r}.",
                status_code=409,
            )
        depends_on = list(ps.depends_on)
        if len(depends_on) != len(set(depends_on)):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Duplicate depends_on on Step {ps.id!r}.",
                status_code=409,
            )
        for dep in depends_on:
            if dep not in top_level_ids:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"Step {ps.id!r} depends on missing {dep!r}.",
                    status_code=409,
                )
        _validate_typed_config(
            ps=ps, step=step, depends_on=depends_on, allow_loop=has_loop_bodies
        )
        deps[ps.id] = tuple(depends_on)
        dependents.setdefault(ps.id, [])
        for dep in depends_on:
            dependents.setdefault(dep, []).append(ps.id)

    # --- Child iteration instances ---
    parent_by_id = {s.id: s for s in top_rows}
    for child in child_rows:
        if child.iteration_no is None or child.iteration_no < 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP body instance iteration_no must be >= 1.",
                status_code=409,
            )
        parent = parent_by_id.get(child.parent_step_id)  # type: ignore[arg-type]
        if parent is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP body instance parent_step_id is invalid.",
                status_code=409,
            )
        if parent.step_type != AuthorableStepType.LOOP.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP body instance parent must be a LOOP Step.",
                status_code=409,
            )
        if parent.parent_step_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP parent must be a top-level Step.",
                status_code=409,
            )
        loop_ps = plan_by_id[parent.step_key]
        _assert_exact_plan_projection(ps=loop_ps, step=parent)
        try:
            template = ExecutionPlanStep.model_validate(child.step_snapshot)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP body instance step_snapshot is invalid.",
                status_code=409,
            ) from exc
        expected_template = plan_by_id.get(template.id)
        if expected_template is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Unknown LOOP body template {template.id!r}.",
                status_code=409,
            )
        if expected_template.model_dump(mode="json") != child.step_snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"LOOP body instance snapshot is not an exact projection "
                    f"of template {template.id!r}."
                ),
                status_code=409,
            )
        body_ids = ownership.loop_to_body.get(parent.step_key, ())
        if template.id not in body_ids:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Template {template.id!r} is not owned by LOOP "
                    f"{parent.step_key!r}."
                ),
                status_code=409,
            )
        if child.step_type != template.type.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP body instance step_type mismatches template.",
                status_code=409,
            )
        if child.step_type not in _BODY_RUNTIME_TYPES:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Unsupported LOOP body step_type {child.step_type!r}.",
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
                message="LOOP body instance step_key is not canonical.",
                status_code=409,
            )
        expected_tv = tool_version_for_template(expected_template)
        if child.mcp_tool_version_id != expected_tv:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="LOOP body TOOL mcp_tool_version_id mismatches template.",
                status_code=409,
            )

        # Runtime deps: body→body same iteration; LOOP entry edge omitted
        # (satisfied while parent LOOP RUNNING — see eligibility).
        runtime_deps: list[str] = []
        plan_deps = list(expected_template.depends_on)
        body_dep_count = 0
        for dep in plan_deps:
            if dep == parent.step_key:
                continue
            if dep in body_ids:
                body_dep_count += 1
                runtime_deps.append(
                    iteration_step_key(
                        parent_step_id=parent.id,
                        iteration_no=child.iteration_no,
                        template_step_id=dep,
                    )
                )
            else:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"LOOP body {template.id!r} has invalid depends_on "
                        f"{dep!r}."
                    ),
                    status_code=409,
                )
        if (
            template.type == AuthorableStepType.TOOL
            and body_dep_count > 1
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"TOOL {template.id!r} direct fan-in is unsupported; "
                    "use an explicit JOIN."
                ),
                status_code=409,
            )
        if template.type == AuthorableStepType.JOIN and not plan_deps:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"JOIN {template.id!r} requires one or more dependencies.",
                status_code=409,
            )
        _validate_typed_config(
            ps=expected_template,
            step=child,
            depends_on=[d for d in plan_deps if d != parent.step_key],
            allow_loop=False,
        )
        deps[child.step_key] = tuple(runtime_deps)
        dependents.setdefault(child.step_key, [])
        for dep_key in runtime_deps:
            dependents.setdefault(dep_key, []).append(child.step_key)

    # Acyclic + full coverage via Kahn on the scheduling keys.
    for k in deps:
        dependents.setdefault(k, [])
    indeg = {k: len(deps[k]) for k in deps}
    queue = [k for k, d in indeg.items() if d == 0]

    # Deterministic order: top-level Plan order, then LOOP children by parent
    # Plan order, iteration_no, body template Plan order.
    plan_order_index = {ps.id: i for i, ps in enumerate(plan.steps)}
    top_order = [ps.id for ps in top_level_plan]

    def _order_key(step_key: str) -> tuple:
        step = by_key[step_key]
        if step.parent_step_id is None:
            return (0, plan_order_index.get(step_key, 10_000), 0, 0)
        parent = parent_by_id[step.parent_step_id]
        template_id = template_id_from_step_snapshot(step)
        return (
            1,
            plan_order_index.get(parent.step_key, 10_000),
            step.iteration_no or 0,
            plan_order_index.get(template_id, 10_000),
        )

    queue.sort(key=_order_key)
    seen: list[str] = []
    while queue:
        cur = queue.pop(0)
        seen.append(cur)
        for nxt in sorted(dependents[cur], key=_order_key):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
                queue.sort(key=_order_key)
    if len(seen) != len(deps):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Runtime DAG contains a cycle or unreachable Steps."
            ),
            status_code=409,
        )

    # Prefer full scheduling order from Kahn; keep top-level Plan order first
    # among equal classes via _order_key.
    ordered = tuple(seen)

    root_tools = tuple(
        key
        for key in top_order
        if not deps.get(key)
        and by_key[key].step_type == AuthorableStepType.TOOL.value
    )

    max_parallelism = int(plan.limits.max_parallelism)
    if max_parallelism < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Plan.limits.max_parallelism must be >= 1.",
            status_code=409,
        )

    return ToolJoinDag(
        ordered_step_keys=ordered,
        root_tool_keys=root_tools,
        max_parallelism=max_parallelism,
        dependents={k: tuple(v) for k, v in dependents.items()},
        dependencies=deps,
    )


def is_intentional_conditional_skip(step: ExecutionStep) -> bool:
    return (
        step.status == StepStatus.SKIPPED.value
        and step.error_code in INTENTIONAL_CONDITIONAL_SKIP_CODES
    )


def dependency_progression_complete(
    *,
    dep_step: ExecutionStep,
    dependent_step: ExecutionStep | None = None,
) -> bool:
    """Whether a dependency unblocks downstream TOOL/CONDITION scheduling.

    Body-instance entry edges treat a parent LOOP that is RUNNING as satisfied.
    Top-level dependents of a LOOP still require LOOP terminal success/failure
    progression (SUCCEEDED or continuable known failure).
    """
    if (
        dependent_step is not None
        and dependent_step.parent_step_id is not None
        and dependent_step.parent_step_id == dep_step.id
        and dep_step.step_type == AuthorableStepType.LOOP.value
        and dep_step.status == StepStatus.RUNNING.value
    ):
        return True
    if dep_step.status == StepStatus.SUCCEEDED.value:
        return True
    # Intentional conditional skips do not unblock non-JOIN descendants.
    if is_intentional_conditional_skip(dep_step):
        return False
    try:
        on_error = plan_step_for(dep_step).on_error
    except Exception:
        return False
    return is_continuable_known_failure(
        on_error=on_error, step_status=dep_step.status
    )


def _parent_loop_running_if_body(
    *,
    step: ExecutionStep,
    by_key: Mapping[str, ExecutionStep],
) -> bool:
    """Body instances require their parent LOOP to be RUNNING."""
    if step.parent_step_id is None:
        return True
    parent = next(
        (s for s in by_key.values() if s.id == step.parent_step_id),
        None,
    )
    return (
        parent is not None
        and parent.step_type == AuthorableStepType.LOOP.value
        and parent.status == StepStatus.RUNNING.value
    )


def has_intentional_skip_dependency(
    *,
    step: ExecutionStep,
    dag: ToolJoinDag,
    by_key: Mapping[str, ExecutionStep],
) -> bool:
    for dep_id in dag.dependencies.get(step.step_key, ()):
        if is_intentional_conditional_skip(by_key[dep_id]):
            return True
    return False


def tool_structurally_eligible(
    *,
    step: ExecutionStep,
    dag: ToolJoinDag,
    by_key: Mapping[str, ExecutionStep],
) -> bool:
    if step.status != StepStatus.PENDING.value:
        return False
    if step.step_type != AuthorableStepType.TOOL.value:
        return False
    if not _parent_loop_running_if_body(step=step, by_key=by_key):
        return False
    if has_intentional_skip_dependency(step=step, dag=dag, by_key=by_key):
        return False
    for dep_id in dag.dependencies.get(step.step_key, ()):
        dep = by_key[dep_id]
        if not dependency_progression_complete(
            dep_step=dep, dependent_step=step
        ):
            return False
    return True


def condition_structurally_eligible(
    *,
    step: ExecutionStep,
    dag: ToolJoinDag,
    by_key: Mapping[str, ExecutionStep],
) -> bool:
    if step.status != StepStatus.PENDING.value:
        return False
    if step.step_type != AuthorableStepType.CONDITION.value:
        return False
    if not _parent_loop_running_if_body(step=step, by_key=by_key):
        return False
    if has_intentional_skip_dependency(step=step, dag=dag, by_key=by_key):
        return False
    for dep_id in dag.dependencies.get(step.step_key, ()):
        dep = by_key[dep_id]
        if not dependency_progression_complete(
            dep_step=dep, dependent_step=step
        ):
            return False
    return True


def approval_structurally_eligible(
    *,
    step: ExecutionStep,
    dag: ToolJoinDag,
    by_key: Mapping[str, ExecutionStep],
) -> bool:
    """PENDING APPROVAL whose direct deps are progression-complete."""
    if step.status != StepStatus.PENDING.value:
        return False
    if step.step_type != AuthorableStepType.APPROVAL.value:
        return False
    if has_intentional_skip_dependency(step=step, dag=dag, by_key=by_key):
        return False
    for dep_id in dag.dependencies.get(step.step_key, ()):
        dep = by_key[dep_id]
        if not dependency_progression_complete(
            dep_step=dep, dependent_step=step
        ):
            return False
    return True


def loop_structurally_eligible(
    *,
    step: ExecutionStep,
    dag: ToolJoinDag,
    by_key: Mapping[str, ExecutionStep],
) -> bool:
    """PENDING top-level LOOP whose direct deps are progression-complete."""
    if step.status != StepStatus.PENDING.value:
        return False
    if step.step_type != AuthorableStepType.LOOP.value:
        return False
    if step.parent_step_id is not None:
        return False
    if has_intentional_skip_dependency(step=step, dag=dag, by_key=by_key):
        return False
    for dep_id in dag.dependencies.get(step.step_key, ()):
        dep = by_key[dep_id]
        if not dependency_progression_complete(
            dep_step=dep, dependent_step=step
        ):
            return False
    return True


def join_barrier_ready(
    *,
    step: ExecutionStep,
    dag: ToolJoinDag,
    by_key: Mapping[str, ExecutionStep],
) -> bool:
    if step.status != StepStatus.PENDING.value:
        return False
    if step.step_type != AuthorableStepType.JOIN.value:
        return False
    if not _parent_loop_running_if_body(step=step, by_key=by_key):
        return False
    deps = dag.dependencies.get(step.step_key, ())
    if not deps:
        return False
    return all(by_key[d].status in _JOIN_BARRIER_TERMINAL for d in deps)


def evaluate_join_policy(
    *,
    policy: JoinPolicy | str,
    dependency_statuses: Sequence[str],
) -> tuple[str, str | None]:
    """Return (SUCCEEDED|FAILED, error_code). Barrier: all deps already terminal."""
    policy_value = policy.value if isinstance(policy, JoinPolicy) else str(policy)
    if policy_value == JoinPolicy.ALL_SUCCESS.value:
        if all(s == StepStatus.SUCCEEDED.value for s in dependency_statuses):
            return StepStatus.SUCCEEDED.value, None
        return StepStatus.FAILED.value, "JOIN_POLICY_UNSATISFIED"
    if policy_value == JoinPolicy.ALL_COMPLETE.value:
        return StepStatus.SUCCEEDED.value, None
    if policy_value == JoinPolicy.ANY_SUCCESS.value:
        if any(s == StepStatus.SUCCEEDED.value for s in dependency_statuses):
            return StepStatus.SUCCEEDED.value, None
        return StepStatus.FAILED.value, "JOIN_POLICY_UNSATISFIED"
    raise AppError(
        code="RESOURCE_CONFLICT",
        message=f"Unsupported JOIN policy {policy_value!r}.",
        status_code=409,
    )


def count_tool_slots(steps: Sequence[ExecutionStep]) -> int:
    return sum(
        1
        for s in steps
        if s.step_type == AuthorableStepType.TOOL.value
        and s.status
        in {StepStatus.READY.value, StepStatus.RUNNING.value}
    )


def skip_all_pending(
    *,
    by_key: Mapping[str, ExecutionStep],
    now: Any,
) -> list[str]:
    """PENDING → SKIPPED for untouched Steps (DAG-wide stop)."""
    skipped: list[str] = []
    for step in by_key.values():
        if step.status != StepStatus.PENDING.value:
            continue
        step.status = StepStatus.SKIPPED.value
        step.error_code = "UPSTREAM_EXECUTION_STOPPED"
        step.error_message = (
            "Upstream Execution stopped; downstream Step was not started."
        )
        step.finished_at = now
        step.lock_version += 1
        skipped.append(step.step_key)
    return skipped


def cancel_unused_ready_tools(
    *,
    by_key: Mapping[str, ExecutionStep],
    now: Any,
) -> list[str]:
    """READY TOOL that never started → CANCELLED (legal transition)."""
    cancelled: list[str] = []
    for step in by_key.values():
        if (
            step.step_type != AuthorableStepType.TOOL.value
            or step.status != StepStatus.READY.value
            or step.started_at is not None
            or step.attempt_count != 0
        ):
            continue
        step.status = StepStatus.CANCELLED.value
        step.error_code = "UPSTREAM_EXECUTION_STOPPED"
        step.error_message = (
            "Upstream Execution stopped before TOOL Attempt start."
        )
        step.finished_at = now
        step.lock_version += 1
        cancelled.append(step.step_key)
    return cancelled


@dataclass(frozen=True, slots=True)
class StopCause:
    """Deterministic stop-causing Step outcome within a wave."""

    step_key: str
    plan_index: int
    kind: str  # FATAL | FAIL_EXECUTION_TIMED_OUT | FAIL_EXECUTION_FAILED
    step_status: str
    error_code: str | None
    error_message: str | None


def stop_cause_precedence(kind: str) -> int:
    order = {
        "FATAL": 0,
        "FAIL_EXECUTION_TIMED_OUT": 1,
        "FAIL_EXECUTION_FAILED": 2,
    }
    return order.get(kind, 99)


def pick_stop_cause(causes: Sequence[StopCause]) -> StopCause | None:
    if not causes:
        return None
    return sorted(
        causes,
        key=lambda c: (stop_cause_precedence(c.kind), c.plan_index, c.step_key),
    )[0]


def is_single_tool_execution(steps: Sequence[ExecutionStep]) -> bool:
    tools = [s for s in steps if s.step_type == AuthorableStepType.TOOL.value]
    joins = [s for s in steps if s.step_type == AuthorableStepType.JOIN.value]
    conditions = [
        s for s in steps if s.step_type == AuthorableStepType.CONDITION.value
    ]
    approvals = [
        s for s in steps if s.step_type == AuthorableStepType.APPROVAL.value
    ]
    loops = [s for s in steps if s.step_type == AuthorableStepType.LOOP.value]
    return (
        len(tools) == 1
        and len(joins) == 0
        and len(conditions) == 0
        and len(approvals) == 0
        and len(loops) == 0
        and len(steps) == 1
    )
