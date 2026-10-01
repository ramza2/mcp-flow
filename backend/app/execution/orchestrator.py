"""TOOL/JOIN DAG wave orchestration (docs/04 §9).

``McpToolRunner`` owns one TOOL Step invocation (ToolCall/Attempt/Step).
``ExecutionOrchestrator`` owns wave scheduling, JOIN reconciliation, ErrorPolicy,
ALL_REQUIRED aggregation, and final Execution terminalization / lease release.

Supported runtime Steps: TOOL + JOIN. CONDITION / LOOP / authorable APPROVAL
Step / Step.when are out of scope.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, ExecutionStatus, StepStatus
from app.execution.completion import (
    DISPOSITION_FATAL_EXECUTION_FAILURE,
    DISPOSITION_KNOWN_STEP_FAILURE,
    DISPOSITION_NOOP,
    DISPOSITION_RETRY,
    DISPOSITION_SUCCESS,
    DISPOSITION_WAIT,
    aggregate_all_required,
    build_result_summary,
    fail_fast_execution_status,
    is_continuable_known_failure,
    plan_step_for,
)
from app.execution.dag import (
    StopCause,
    ToolJoinDag,
    cancel_unused_ready_tools,
    count_tool_slots,
    evaluate_join_policy,
    is_single_tool_execution,
    join_barrier_ready,
    pick_stop_cause,
    skip_all_pending,
    tool_structurally_eligible,
    validate_tool_join_dag,
)
from app.models.execution import Execution, ExecutionStep
from app.repositories.execution import ExecutionRepository
from app.schemas.execution_plan import (
    ComplexToolStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    JoinStepConfigV1,
    compute_plan_hash,
)

logger = logging.getLogger(__name__)

_REASON_SAFE_RETRY_READY = "SAFE_RETRY_READY"
_REASON_WAITING_INPUT = "WAITING_INPUT"
_REASON_EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
_REASON_EXECUTION_PARTIAL = "EXECUTION_PARTIALLY_SUCCEEDED"
_REASON_EXECUTION_FAILED = "EXECUTION_FAILED"
_REASON_EXECUTION_TIMED_OUT = "EXECUTION_TIMED_OUT"
_REASON_WAVE_IN_PROGRESS = "WAVE_IN_PROGRESS"

_STEP_TERMINAL = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.UNKNOWN_OUTCOME.value,
        StepStatus.CANCELLED.value,
        StepStatus.SKIPPED.value,
    }
)


@dataclass(frozen=True, slots=True)
class SequentialToolChain:
    """Validated linear TOOL chain (compatibility helper for #44 tests)."""

    root_step_key: str
    ordered_step_keys: tuple[str, ...]


def validate_sequential_tool_chain(
    plan: ExecutionPlanV1,
    steps: list[ExecutionStep],
) -> SequentialToolChain:
    """Fail closed unless Steps form a single-root linear TOOL-only chain."""
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

    plan_by_id = {ps.id: ps for ps in plan.steps}
    if set(plan_by_id) != set(by_key):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ExecutionStep keys do not match plan step ids.",
            status_code=409,
        )

    depends: dict[str, list[str]] = {}
    for step in steps:
        if step.step_type != AuthorableStepType.TOOL.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Sequential orchestration supports TOOL Steps only; "
                    f"found {step.step_type!r} ({step.step_key!r})."
                ),
                status_code=409,
            )
        if step.parent_step_id is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="parent_step_id must be null for sequential TOOL orchestration.",
                status_code=409,
            )
        try:
            plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
            expected = plan_by_id[step.step_key]
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Step snapshot invalid for {step.step_key!r}.",
                status_code=409,
            ) from exc
        if expected.model_dump(mode="json") != step.step_snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Step snapshot lineage mismatch for {step.step_key!r}.",
                status_code=409,
            )
        if (
            plan_step.id != step.step_key
            or plan_step.type != AuthorableStepType.TOOL
            or plan_step.when is not None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Step {step.step_key!r} is not a readyable sequential TOOL.",
                status_code=409,
            )
        try:
            cfg = ComplexToolStepConfigV1.model_validate(plan_step.config)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"TOOL config for {step.step_key!r} must be a valid "
                    "ComplexToolStepConfigV1."
                ),
                status_code=409,
            ) from exc
        if step.mcp_tool_version_id != cfg.tool_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"mcp_tool_version_id mismatch for {step.step_key!r}.",
                status_code=409,
            )
        deps = list(plan_step.depends_on)
        if len(deps) != len(set(deps)):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"duplicate depends_on on {step.step_key!r}.",
                status_code=409,
            )
        if len(deps) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Sequential orchestration forbids fan-in "
                    f"(step {step.step_key!r} has {len(deps)} dependencies)."
                ),
                status_code=409,
            )
        for dep in deps:
            if dep not in by_key:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=f"depends_on={dep!r} missing for {step.step_key!r}.",
                    status_code=409,
                )
        depends[step.step_key] = deps

    roots = [k for k, deps in depends.items() if not deps]
    if len(roots) != 1:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Sequential orchestration requires exactly one root TOOL Step; "
                f"found {len(roots)}."
            ),
            status_code=409,
        )

    dependents: dict[str, list[str]] = {k: [] for k in depends}
    for key, deps in depends.items():
        for dep in deps:
            dependents[dep].append(key)
    for key, children in dependents.items():
        if len(children) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Sequential orchestration forbids fan-out "
                    f"(step {key!r} has {len(children)} dependents)."
                ),
                status_code=409,
            )

    ordered: list[str] = []
    current = roots[0]
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Sequential TOOL chain contains a cycle.",
                status_code=409,
            )
        seen.add(current)
        ordered.append(current)
        children = dependents[current]
        current = children[0] if children else None

    if len(ordered) != len(steps):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Sequential TOOL chain does not cover all ExecutionSteps.",
            status_code=409,
        )

    return SequentialToolChain(
        root_step_key=ordered[0],
        ordered_step_keys=tuple(ordered),
    )


def assert_execution_plan_lineage(execution: Execution) -> ExecutionPlanV1:
    try:
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan snapshot is invalid.",
            status_code=409,
        ) from exc
    if (
        execution.plan_schema_version != plan.schema_version
        or compute_plan_hash(execution.plan_snapshot) != execution.plan_hash
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution plan snapshot/hash lineage is inconsistent.",
            status_code=409,
        )
    return plan


@dataclass(frozen=True, slots=True)
class ProgressOutcome:
    execution_complete: bool
    promoted: bool
    reason: str
    execution_status: str | None = None
    ready_step_ids: tuple[uuid.UUID, ...] = ()
    plan_order: tuple[str, ...] = ()
    single_tool: bool = False
    # Prefer Step-level terminal (e.g. UNKNOWN_OUTCOME) over Execution FAILED.
    step_terminal_status: str | None = None


class ExecutionOrchestrator:
    """Deterministic TOOL/JOIN wave scheduler under one Execution lease."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        tool_runner: Any,
    ) -> None:
        self._session_factory = session_factory
        self._runner = tool_runner

    async def run(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
    ) -> Any:
        from app.execution.tool_runner import ToolRunOutcome

        last_outcome: Any = None
        while True:
            prepared = await self._prepare_wave(
                execution_id=execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
            if prepared.reason == "MISSING":
                return ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=None,
                    attempt_id=None,
                    tool_call_id=None,
                    mcp_called=False,
                    terminal_status=None,
                    reason="MISSING",
                )
            if prepared.reason == "STALE_LEASE":
                return ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=None,
                    attempt_id=None,
                    tool_call_id=None,
                    mcp_called=False,
                    terminal_status=prepared.execution_status,
                    reason="LEASE_MISMATCH",
                )
            if prepared.reason == _REASON_WAVE_IN_PROGRESS:
                return ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=None,
                    attempt_id=None,
                    tool_call_id=None,
                    mcp_called=False,
                    terminal_status=None,
                    reason=_REASON_WAVE_IN_PROGRESS,
                )
            if prepared.execution_complete:
                return ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=(
                        last_outcome.step_execution_id if last_outcome else None
                    ),
                    attempt_id=last_outcome.attempt_id if last_outcome else None,
                    tool_call_id=last_outcome.tool_call_id if last_outcome else None,
                    mcp_called=bool(last_outcome.mcp_called) if last_outcome else False,
                    terminal_status=(
                        prepared.step_terminal_status or prepared.execution_status
                    ),
                    reason=prepared.reason,
                )
            if prepared.reason == "WAITING_HANDOFF" and prepared.ready_step_ids:
                # Single-TOOL wait path — preserve existing runner wait semantics.
                return await self._runner.run_claimed_tool_step(
                    execution_id=execution_id,
                    step_execution_id=prepared.ready_step_ids[0],
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            if not prepared.ready_step_ids:
                # Idle under lease with no READY TOOL — finalize or return.
                finalized = await self._finalize_natural_end(
                    execution_id=execution_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
                if finalized is not None:
                    return ToolRunOutcome(
                        execution_id=execution_id,
                        step_execution_id=None,
                        attempt_id=None,
                        tool_call_id=None,
                        mcp_called=False,
                        terminal_status=finalized.execution_status,
                        reason=finalized.reason,
                    )
                return last_outcome or ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=None,
                    attempt_id=None,
                    tool_call_id=None,
                    mcp_called=False,
                    terminal_status=None,
                    reason="LEASE_MISMATCH",
                )

            single_tool = prepared.single_tool
            wave_ids = list(prepared.ready_step_ids)
            outcomes = await asyncio.gather(
                *[
                    self._runner.run_claimed_tool_step(
                        execution_id=execution_id,
                        step_execution_id=sid,
                        worker_id=worker_id,
                        lease_token=lease_token,
                        defer_execution_terminalization=not single_tool,
                        forbid_execution_wait=not single_tool,
                    )
                    for sid in wave_ids
                ]
            )
            if outcomes:
                last_outcome = outcomes[-1]

            # Single-TOOL wait/retry dispositions — preserve #46 entry behavior.
            if single_tool and len(outcomes) == 1:
                outcome = outcomes[0]
                disposition = self._effective_disposition(outcome)
                if disposition == DISPOSITION_RETRY or outcome.reason == _REASON_SAFE_RETRY_READY:
                    continue
                if disposition == DISPOSITION_WAIT or outcome.reason in {
                    "WAITING_APPROVAL",
                    _REASON_WAITING_INPUT,
                }:
                    return outcome
                if outcome.reason in {
                    "LEASE_MISMATCH",
                    "MISSING",
                    "STEP_ALREADY_TERMINAL",
                    "RESOURCE_CONFLICT",
                    "TOOL_CALL_ALREADY_STARTED",
                }:
                    return outcome

            settled = await self._settle_wave(
                execution_id=execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
                wave_step_ids=tuple(wave_ids),
                outcomes=list(outcomes),
                plan_order=prepared.plan_order,
            )
            if settled.execution_complete:
                return ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=(
                        last_outcome.step_execution_id if last_outcome else None
                    ),
                    attempt_id=last_outcome.attempt_id if last_outcome else None,
                    tool_call_id=last_outcome.tool_call_id if last_outcome else None,
                    mcp_called=any(o.mcp_called for o in outcomes),
                    terminal_status=(
                        settled.step_terminal_status
                        or settled.execution_status
                        or (last_outcome.terminal_status if last_outcome else None)
                    ),
                    reason=settled.reason,
                )
            # Next wave.
            continue

    @staticmethod
    def _effective_disposition(outcome: Any) -> str:
        if outcome.disposition and outcome.disposition != DISPOSITION_NOOP:
            return outcome.disposition
        if outcome.reason == _REASON_SAFE_RETRY_READY:
            return DISPOSITION_RETRY
        if outcome.reason in {"WAITING_APPROVAL", _REASON_WAITING_INPUT}:
            return DISPOSITION_WAIT
        if outcome.terminal_status == StepStatus.SUCCEEDED.value:
            return DISPOSITION_SUCCESS
        if outcome.terminal_status == StepStatus.UNKNOWN_OUTCOME.value:
            return DISPOSITION_FATAL_EXECUTION_FAILURE
        if outcome.terminal_status in {
            StepStatus.FAILED.value,
            StepStatus.TIMED_OUT.value,
        }:
            return DISPOSITION_KNOWN_STEP_FAILURE
        return DISPOSITION_NOOP

    async def _prepare_wave(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
    ) -> ProgressOutcome:
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(execution_id)
                if execution is None:
                    return ProgressOutcome(
                        execution_complete=False, promoted=False, reason="MISSING"
                    )
                now = datetime.now(UTC)
                if execution.status in {
                    ExecutionStatus.SUCCEEDED.value,
                    ExecutionStatus.PARTIALLY_SUCCEEDED.value,
                    ExecutionStatus.FAILED.value,
                    ExecutionStatus.TIMED_OUT.value,
                    ExecutionStatus.CANCELLED.value,
                }:
                    return ProgressOutcome(
                        execution_complete=True,
                        promoted=False,
                        reason="STEP_ALREADY_TERMINAL",
                        execution_status=execution.status,
                    )
                steps = await executions.list_steps(execution.id)
                waiting = [
                    s
                    for s in steps
                    if s.status
                    in {
                        StepStatus.WAITING_APPROVAL.value,
                        StepStatus.WAITING_INPUT.value,
                    }
                ]
                # Durable wait or one-sided wait corruption — ToolRunner owns
                # classification (including RESOURCE_CONFLICT). Do not require a
                # live Execution lease here; WAITING_* clears the lease.
                if waiting or execution.status in {
                    ExecutionStatus.WAITING_APPROVAL.value,
                    ExecutionStatus.WAITING_INPUT.value,
                }:
                    target = waiting[0] if waiting else next(
                        (
                            s
                            for s in steps
                            if s.step_type == AuthorableStepType.TOOL.value
                        ),
                        None,
                    )
                    if target is not None:
                        return ProgressOutcome(
                            execution_complete=False,
                            promoted=False,
                            reason="WAITING_HANDOFF",
                            ready_step_ids=(target.id,),
                        )
                if not self._has_running_lease(
                    execution, worker_id, lease_token, now
                ):
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason="STALE_LEASE",
                        execution_status=execution.status,
                    )

                plan = assert_execution_plan_lineage(execution)
                dag = validate_tool_join_dag(plan, steps)
                by_key = {s.step_key: s for s in steps}
                single_tool = is_single_tool_execution(steps)

                # Duplicate delivery: another wave already RUNNING under lease.
                running_tools = [
                    s
                    for s in steps
                    if s.step_type == AuthorableStepType.TOOL.value
                    and s.status == StepStatus.RUNNING.value
                ]
                if running_tools:
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason=_REASON_WAVE_IN_PROGRESS,
                        plan_order=dag.ordered_step_keys,
                    )

                # Reconcile JOINs before reserving TOOL slots.
                join_stop = await self._reconcile_joins_locked(
                    executions=executions,
                    execution=execution,
                    plan=plan,
                    dag=dag,
                    steps=steps,
                    by_key=by_key,
                    now=now,
                )
                if join_stop is not None:
                    await session.flush()
                    return join_stop

                # Refresh after JOIN mutations.
                steps = await executions.list_steps(execution.id)
                by_key = {s.step_key: s for s in steps}

                ready_ids, newly_promoted = await self._reserve_tool_wave_locked(
                    executions=executions,
                    execution=execution,
                    dag=dag,
                    steps=steps,
                    by_key=by_key,
                    now=now,
                )
                await session.flush()
                return ProgressOutcome(
                    execution_complete=False,
                    promoted=bool(newly_promoted),
                    reason="WAVE_READY" if ready_ids else "NO_READY",
                    ready_step_ids=tuple(ready_ids),
                    plan_order=dag.ordered_step_keys,
                    single_tool=single_tool,
                )

    async def _reserve_tool_wave_locked(
        self,
        *,
        executions: ExecutionRepository,
        execution: Execution,
        dag: ToolJoinDag,
        steps: list[ExecutionStep],
        by_key: dict[str, ExecutionStep],
        now: datetime,
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Return (wave_ready_ids, newly_promoted_ids)."""
        # Already READY TOOLs are part of the wave (claim-time roots).
        already_ready = [
            by_key[k]
            for k in dag.ordered_step_keys
            if by_key[k].step_type == AuthorableStepType.TOOL.value
            and by_key[k].status == StepStatus.READY.value
        ]
        used = count_tool_slots(steps)
        available = max(0, dag.max_parallelism - used)
        eligible = [
            by_key[k]
            for k in dag.ordered_step_keys
            if tool_structurally_eligible(
                step=by_key[k], dag=dag, by_key=by_key
            )
        ]
        promoted: list[ExecutionStep] = []
        for step in eligible:
            if available <= 0:
                break
            locked = await executions.lock_step(step.id)
            if locked is None or locked.status != StepStatus.PENDING.value:
                continue
            locked.status = StepStatus.READY.value
            locked.ready_at = now
            locked.lock_version += 1
            promoted.append(locked)
            available -= 1
            logger.info(
                "orchestrator reserved TOOL execution_id=%s step_key=%s",
                execution.id,
                locked.step_key,
            )

        execution.heartbeat_at = now
        execution.lock_version += 1
        # Wave = already READY + newly promoted (Plan order).
        wave_keys = {
            s.step_key for s in already_ready
        } | {s.step_key for s in promoted}
        ready_ids = [
            by_key[k].id
            for k in dag.ordered_step_keys
            if k in wave_keys
            and by_key[k].status == StepStatus.READY.value
        ]
        return ready_ids, [s.id for s in promoted]

    async def _reconcile_joins_locked(
        self,
        *,
        executions: ExecutionRepository,
        execution: Execution,
        plan: ExecutionPlanV1,
        dag: ToolJoinDag,
        steps: list[ExecutionStep],
        by_key: dict[str, ExecutionStep],
        now: datetime,
    ) -> ProgressOutcome | None:
        """Evaluate barrier-ready JOINs. May terminalize Execution on FAIL_EXECUTION."""
        for key in dag.ordered_step_keys:
            step = by_key[key]
            if not join_barrier_ready(step=step, dag=dag, by_key=by_key):
                continue
            locked = await executions.lock_step(step.id)
            if locked is None or locked.status != StepStatus.PENDING.value:
                continue
            cfg = JoinStepConfigV1.model_validate(
                ExecutionPlanStep.model_validate(locked.step_snapshot).config
            )
            dep_statuses = [
                by_key[d].status for d in dag.dependencies[locked.step_key]
            ]
            terminal, error_code = evaluate_join_policy(
                policy=cfg.policy, dependency_statuses=dep_statuses
            )
            # Local PENDING→READY→RUNNING→terminal in one TX.
            locked.status = StepStatus.READY.value
            locked.ready_at = now
            locked.status = StepStatus.RUNNING.value
            if locked.started_at is None:
                locked.started_at = now
            locked.status = terminal
            locked.error_code = error_code
            locked.error_message = (
                "JOIN policy was not satisfied."
                if error_code is not None
                else None
            )
            locked.finished_at = now
            locked.lock_version += 1
            by_key[locked.step_key] = locked
            logger.info(
                "orchestrator JOIN terminal execution_id=%s step_key=%s status=%s",
                execution.id,
                locked.step_key,
                terminal,
            )

            if terminal == StepStatus.SUCCEEDED.value:
                continue
            on_error = plan_step_for(locked).on_error
            if on_error == "FAIL_EXECUTION" or not is_continuable_known_failure(
                on_error=on_error, step_status=terminal
            ):
                # Fail-fast stop from JOIN.
                skip_all_pending(by_key=by_key, now=now)
                cancel_unused_ready_tools(by_key=by_key, now=now)
                exec_status = fail_fast_execution_status(terminal)
                execution.status = exec_status
                execution.error_code = locked.error_code
                execution.error_message = locked.error_message
                refreshed = list(by_key.values())
                execution.result_summary = build_result_summary(
                    status=exec_status, steps=refreshed, plan=plan
                )
                execution.finished_at = now
                execution.worker_id = None
                execution.lease_token = None
                execution.lease_expires_at = None
                execution.heartbeat_at = None
                execution.lock_version += 1
                return ProgressOutcome(
                    execution_complete=True,
                    promoted=False,
                    reason=_REASON_EXECUTION_FAILED
                    if exec_status == ExecutionStatus.FAILED.value
                    else _REASON_EXECUTION_TIMED_OUT,
                    execution_status=exec_status,
                    step_terminal_status=terminal,
                )
            # Continuable JOIN failure — continue reconciling.
        return None

    async def _settle_wave(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
        wave_step_ids: tuple[uuid.UUID, ...],
        outcomes: list[Any],
        plan_order: tuple[str, ...],
    ) -> ProgressOutcome:
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(execution_id)
                if execution is None:
                    return ProgressOutcome(
                        execution_complete=False, promoted=False, reason="MISSING"
                    )
                now = datetime.now(UTC)
                steps = await executions.list_steps(execution.id)
                by_key = {s.step_key: s for s in steps}
                by_id = {s.id: s for s in steps}
                plan_index = {sid: i for i, sid in enumerate(plan_order)}

                causes: list[StopCause] = []
                for step_id, outcome in zip(wave_step_ids, outcomes, strict=False):
                    step = by_id.get(step_id)
                    if step is None:
                        continue
                    disposition = self._effective_disposition(outcome)
                    if disposition == DISPOSITION_FATAL_EXECUTION_FAILURE or (
                        outcome.terminal_status == StepStatus.UNKNOWN_OUTCOME.value
                    ):
                        causes.append(
                            StopCause(
                                step_key=step.step_key,
                                plan_index=plan_index.get(step.step_key, 10**9),
                                kind="FATAL",
                                step_status=outcome.terminal_status
                                or StepStatus.FAILED.value,
                                error_code=step.error_code,
                                error_message=step.error_message,
                            )
                        )
                        continue
                    if disposition != DISPOSITION_KNOWN_STEP_FAILURE:
                        continue
                    try:
                        on_error = plan_step_for(step).on_error
                    except Exception:
                        on_error = "FAIL_EXECUTION"
                    term = outcome.terminal_status or step.status
                    if on_error == "FAIL_EXECUTION" or not is_continuable_known_failure(
                        on_error=on_error, step_status=term
                    ):
                        kind = (
                            "FAIL_EXECUTION_TIMED_OUT"
                            if term == StepStatus.TIMED_OUT.value
                            else "FAIL_EXECUTION_FAILED"
                        )
                        causes.append(
                            StopCause(
                                step_key=step.step_key,
                                plan_index=plan_index.get(step.step_key, 10**9),
                                kind=kind,
                                step_status=term,
                                error_code=step.error_code,
                                error_message=step.error_message,
                            )
                        )

                stop = pick_stop_cause(causes)
                if stop is not None:
                    # Fatal / fail-fast after wave settlement — Plan parse optional.
                    skip_all_pending(by_key=by_key, now=now)
                    cancel_unused_ready_tools(by_key=by_key, now=now)
                    if stop.kind == "FATAL":
                        exec_status = ExecutionStatus.FAILED.value
                    else:
                        exec_status = fail_fast_execution_status(stop.step_status)
                    execution.status = exec_status
                    execution.error_code = stop.error_code
                    execution.error_message = stop.error_message
                    try:
                        plan = assert_execution_plan_lineage(execution)
                    except AppError:
                        plan = None
                    execution.result_summary = build_result_summary(
                        status=exec_status,
                        steps=list(by_key.values()),
                        plan=plan,
                    )
                    execution.finished_at = now
                    execution.worker_id = None
                    execution.lease_token = None
                    execution.lease_expires_at = None
                    execution.heartbeat_at = None
                    execution.lock_version += 1
                    await session.flush()
                    stop_reason = stop.error_code or (
                        _REASON_EXECUTION_TIMED_OUT
                        if exec_status == ExecutionStatus.TIMED_OUT.value
                        else _REASON_EXECUTION_FAILED
                    )
                    return ProgressOutcome(
                        execution_complete=True,
                        promoted=False,
                        reason=stop_reason,
                        execution_status=exec_status,
                        step_terminal_status=stop.step_status,
                    )

                if not self._has_running_lease(
                    execution, worker_id, lease_token, now
                ):
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason="STALE_LEASE",
                    )

                # Continuable wave — reconcile JOINs then maybe complete.
                try:
                    plan = assert_execution_plan_lineage(execution)
                    dag = validate_tool_join_dag(plan, steps)
                except AppError:
                    # Corrupt lineage after known success path — fail closed.
                    execution.status = ExecutionStatus.FAILED.value
                    execution.error_code = "RESOURCE_CONFLICT"
                    execution.error_message = (
                        "Execution plan lineage corrupt after wave settlement."
                    )
                    skip_all_pending(by_key=by_key, now=now)
                    cancel_unused_ready_tools(by_key=by_key, now=now)
                    execution.finished_at = now
                    execution.worker_id = None
                    execution.lease_token = None
                    execution.lease_expires_at = None
                    execution.heartbeat_at = None
                    execution.lock_version += 1
                    await session.flush()
                    return ProgressOutcome(
                        execution_complete=True,
                        promoted=False,
                        reason=_REASON_EXECUTION_FAILED,
                        execution_status=ExecutionStatus.FAILED.value,
                    )

                join_stop = await self._reconcile_joins_locked(
                    executions=executions,
                    execution=execution,
                    plan=plan,
                    dag=dag,
                    steps=steps,
                    by_key=by_key,
                    now=now,
                )
                if join_stop is not None:
                    await session.flush()
                    return join_stop

                steps = await executions.list_steps(execution.id)
                if self._dag_naturally_ended(steps):
                    decision = aggregate_all_required(plan=plan, steps=steps)
                    self._apply_execution_terminal(
                        execution, steps, plan, decision, now
                    )
                    await session.flush()
                    return ProgressOutcome(
                        execution_complete=True,
                        promoted=False,
                        reason=self._completion_reason(decision.status),
                        execution_status=decision.status,
                    )

                execution.heartbeat_at = now
                execution.lock_version += 1
                await session.flush()
                return ProgressOutcome(
                    execution_complete=False,
                    promoted=False,
                    reason="WAVE_SETTLED",
                )

    async def _finalize_natural_end(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
    ) -> ProgressOutcome | None:
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(execution_id)
                if execution is None:
                    return None
                if execution.status in {
                    ExecutionStatus.SUCCEEDED.value,
                    ExecutionStatus.PARTIALLY_SUCCEEDED.value,
                }:
                    return ProgressOutcome(
                        execution_complete=True,
                        promoted=False,
                        reason="ALREADY_COMPLETE",
                        execution_status=execution.status,
                    )
                now = datetime.now(UTC)
                if not self._has_running_lease(
                    execution, worker_id, lease_token, now
                ):
                    return None
                steps = await executions.list_steps(execution.id)
                if not steps:
                    return None
                plan = assert_execution_plan_lineage(execution)
                dag = validate_tool_join_dag(plan, steps)
                by_key = {s.step_key: s for s in steps}
                join_stop = await self._reconcile_joins_locked(
                    executions=executions,
                    execution=execution,
                    plan=plan,
                    dag=dag,
                    steps=steps,
                    by_key=by_key,
                    now=now,
                )
                if join_stop is not None:
                    await session.flush()
                    return join_stop
                steps = await executions.list_steps(execution.id)
                if not self._dag_naturally_ended(steps):
                    return None
                decision = aggregate_all_required(plan=plan, steps=steps)
                self._apply_execution_terminal(
                    execution, steps, plan, decision, now
                )
                await session.flush()
                return ProgressOutcome(
                    execution_complete=True,
                    promoted=False,
                    reason=self._completion_reason(decision.status),
                    execution_status=decision.status,
                )

    # Compatibility wrappers used by #44/#46 unit tests.
    async def _progress_after_terminal_step(
        self,
        *,
        execution_id: uuid.UUID,
        completed_step_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
        allow_continuable_failure: bool,
    ) -> ProgressOutcome:
        """Linear-compatible promote used by existing sequential unit tests."""
        del allow_continuable_failure
        prepared = await self._prepare_wave(
            execution_id=execution_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        if prepared.reason in {"STALE_LEASE", "MISSING"}:
            return prepared
        if prepared.execution_complete:
            return prepared
        # Ensure completed step was considered; promotion happens in prepare.
        async with self._session_factory() as session:
            step = await ExecutionRepository(session).get_step(completed_step_id)
            if step is None:
                return ProgressOutcome(
                    execution_complete=False, promoted=False, reason="MISSING"
                )
        if prepared.ready_step_ids:
            if prepared.promoted:
                return ProgressOutcome(
                    execution_complete=False,
                    promoted=True,
                    reason="PROMOTED",
                    ready_step_ids=prepared.ready_step_ids,
                )
            return ProgressOutcome(
                execution_complete=False,
                promoted=False,
                reason="ALREADY_READY",
                ready_step_ids=prepared.ready_step_ids,
            )
        if prepared.reason == "NO_READY":
            # Maybe already READY from prior promote.
            async with self._session_factory() as session:
                steps = await ExecutionRepository(session).list_steps(execution_id)
                ready = [
                    s
                    for s in steps
                    if s.status == StepStatus.READY.value
                    and s.step_type == AuthorableStepType.TOOL.value
                ]
            if ready:
                return ProgressOutcome(
                    execution_complete=False,
                    promoted=False,
                    reason="ALREADY_READY",
                )
        return ProgressOutcome(
            execution_complete=False,
            promoted=False,
            reason=prepared.reason,
        )

    @staticmethod
    def _dag_naturally_ended(steps: list[ExecutionStep]) -> bool:
        for step in steps:
            if step.status not in _STEP_TERMINAL:
                return False
        return True

    @staticmethod
    def _completion_reason(status: str) -> str:
        if status == ExecutionStatus.SUCCEEDED.value:
            return _REASON_EXECUTION_SUCCEEDED
        if status == ExecutionStatus.PARTIALLY_SUCCEEDED.value:
            return _REASON_EXECUTION_PARTIAL
        if status == ExecutionStatus.TIMED_OUT.value:
            return _REASON_EXECUTION_TIMED_OUT
        return _REASON_EXECUTION_FAILED

    @staticmethod
    def _has_running_lease(
        execution: Execution,
        worker_id: str,
        lease_token: uuid.UUID,
        now: datetime,
    ) -> bool:
        expires = execution.lease_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        elif expires is not None:
            expires = expires.astimezone(UTC)
        return (
            execution.status == ExecutionStatus.RUNNING.value
            and execution.worker_id == worker_id
            and execution.lease_token == lease_token
            and expires is not None
            and expires > now
        )

    @staticmethod
    def _apply_execution_terminal(
        execution: Execution,
        steps: list[ExecutionStep],
        plan: ExecutionPlanV1,
        decision: Any,
        now: datetime,
    ) -> None:
        execution.status = decision.status
        execution.error_code = decision.error_code
        execution.error_message = decision.error_message
        execution.result_summary = build_result_summary(
            status=decision.status, steps=steps, plan=plan
        )
        execution.finished_at = now
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.lock_version += 1
