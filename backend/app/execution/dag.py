"""TOOL/JOIN DAG runtime validation and local JOIN evaluation (docs/04 §9).

Wave scheduling lives in ``ExecutionOrchestrator``; this module is pure graph
logic (no DB writes except callers applying returned decisions).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, JoinPolicy, StepStatus
from app.execution.completion import is_continuable_known_failure, plan_step_for
from app.models.execution import ExecutionStep
from app.schemas.execution_plan import (
    ComplexToolStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    JoinStepConfigV1,
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

_UNSUPPORTED_RUNTIME_TYPES = frozenset(
    {
        AuthorableStepType.CONDITION.value,
        AuthorableStepType.APPROVAL.value,
        AuthorableStepType.LOOP.value,
    }
)


@dataclass(frozen=True, slots=True)
class ToolJoinDag:
    """Validated TOOL/JOIN runtime DAG."""

    ordered_step_keys: tuple[str, ...]  # immutable Plan order
    root_tool_keys: tuple[str, ...]
    max_parallelism: int
    dependents: Mapping[str, tuple[str, ...]]
    dependencies: Mapping[str, tuple[str, ...]]


def validate_tool_join_dag(
    plan: ExecutionPlanV1,
    steps: Sequence[ExecutionStep],
) -> ToolJoinDag:
    """Fail closed unless Steps form a TOOL/JOIN DAG for wave runtime."""
    if not steps:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution has no Steps to orchestrate.",
            status_code=409,
        )
    if len(plan.steps) != len(steps):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution Step count does not match plan.steps.",
            status_code=409,
        )

    by_key = {s.step_key: s for s in steps}
    if len(by_key) != len(steps):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ExecutionStep step_key values are not unique.",
            status_code=409,
        )
    plan_ids = {ps.id for ps in plan.steps}
    if set(by_key) != plan_ids:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ExecutionStep keys do not exactly match Plan Step IDs.",
            status_code=409,
        )

    deps: dict[str, tuple[str, ...]] = {}
    dependents: dict[str, list[str]] = {ps.id: [] for ps in plan.steps}

    for ps in plan.steps:
        step = by_key[ps.id]
        if step.parent_step_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Runtime DAG Steps must have parent_step_id null.",
                status_code=409,
            )
        if step.step_type in _UNSUPPORTED_RUNTIME_TYPES or ps.type.value in (
            AuthorableStepType.CONDITION.value,
            AuthorableStepType.APPROVAL.value,
            AuthorableStepType.LOOP.value,
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Step type {step.step_type!r} unsupported by TOOL/JOIN "
                    "DAG runtime."
                ),
                status_code=409,
            )
        if step.step_type not in {
            AuthorableStepType.TOOL.value,
            AuthorableStepType.JOIN.value,
        }:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Unsupported runtime step_type {step.step_type!r}.",
                status_code=409,
            )

        try:
            parsed = ExecutionPlanStep.model_validate(step.step_snapshot)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"step_snapshot invalid for {ps.id!r}.",
                status_code=409,
            ) from exc
        if parsed.model_dump(mode="json") != step.step_snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"step_snapshot drift for {ps.id!r}.",
                status_code=409,
            )
        if parsed.id != step.step_key or parsed.id != ps.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Step identity mismatch for {ps.id!r}.",
                status_code=409,
            )
        if parsed.when is not None or ps.when is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Step.when is unsupported in TOOL/JOIN DAG runtime.",
                status_code=409,
            )
        if len(parsed.depends_on) != len(set(parsed.depends_on)):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Duplicate depends_on on Step {ps.id!r}.",
                status_code=409,
            )
        for dep in parsed.depends_on:
            if dep not in plan_ids:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"Step {ps.id!r} depends on missing {dep!r}.",
                    status_code=409,
                )
            dependents[dep].append(ps.id)

        if step.step_type == AuthorableStepType.TOOL.value:
            if step.mcp_tool_version_id is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"TOOL Step {ps.id!r} missing mcp_tool_version_id.",
                    status_code=409,
                )
            try:
                cfg = ComplexToolStepConfigV1.model_validate(parsed.config)
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
            if len(parsed.depends_on) > 1:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"TOOL {ps.id!r} direct fan-in is unsupported; "
                        "use an explicit JOIN."
                    ),
                    status_code=409,
                )
        else:
            # JOIN
            if step.mcp_tool_version_id is not None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"JOIN Step {ps.id!r} must have null mcp_tool_version_id.",
                    status_code=409,
                )
            try:
                JoinStepConfigV1.model_validate(parsed.config)
            except Exception as exc:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"Invalid JOIN config for {ps.id!r}.",
                    status_code=409,
                ) from exc
            if not parsed.depends_on:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"JOIN {ps.id!r} requires one or more dependencies.",
                    status_code=409,
                )

        deps[ps.id] = tuple(parsed.depends_on)

    # Acyclic + full coverage via Kahn.
    indeg = {k: len(deps[k]) for k in deps}
    queue = [k for k, d in indeg.items() if d == 0]
    # Prefer Plan order among roots.
    plan_order = [ps.id for ps in plan.steps]
    order_index = {sid: i for i, sid in enumerate(plan_order)}
    queue.sort(key=lambda k: order_index[k])
    seen: list[str] = []
    while queue:
        cur = queue.pop(0)
        seen.append(cur)
        for nxt in sorted(dependents[cur], key=lambda k: order_index[k]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
                queue.sort(key=lambda k: order_index[k])
    if len(seen) != len(deps):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="TOOL/JOIN DAG contains a cycle or unreachable Steps.",
            status_code=409,
        )

    root_tools = tuple(
        ps.id
        for ps in plan.steps
        if not deps[ps.id] and by_key[ps.id].step_type == AuthorableStepType.TOOL.value
    )
    if not root_tools:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="TOOL/JOIN DAG requires at least one root TOOL Step.",
            status_code=409,
        )

    max_parallelism = int(plan.limits.max_parallelism)
    if max_parallelism < 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Plan.limits.max_parallelism must be >= 1.",
            status_code=409,
        )

    return ToolJoinDag(
        ordered_step_keys=tuple(plan_order),
        root_tool_keys=root_tools,
        max_parallelism=max_parallelism,
        dependents={k: tuple(v) for k, v in dependents.items()},
        dependencies=deps,
    )


def dependency_progression_complete(
    *,
    dep_step: ExecutionStep,
) -> bool:
    """Whether a dependency unblocks downstream scheduling."""
    if dep_step.status == StepStatus.SUCCEEDED.value:
        return True
    try:
        on_error = plan_step_for(dep_step).on_error
    except Exception:
        return False
    return is_continuable_known_failure(
        on_error=on_error, step_status=dep_step.status
    )


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
    for dep_id in dag.dependencies.get(step.step_key, ()):
        dep = by_key[dep_id]
        if not dependency_progression_complete(dep_step=dep):
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
    return len(tools) == 1 and len(joins) == 0 and len(steps) == 1
