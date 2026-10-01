"""TOOL/CONDITION/JOIN/APPROVAL/LOOP DAG wave orchestration (docs/04 §9).

``McpToolRunner`` owns one TOOL Step invocation (ToolCall/Attempt/Step).
``ExecutionOrchestrator`` owns wave scheduling, local CONDITION/when/JOIN/
authorable APPROVAL / flat FOR_EACH/WHILE LOOP reconciliation, ErrorPolicy,
ALL_REQUIRED aggregation, and final Execution terminalization / lease release.

Supported runtime Steps: TOOL + CONDITION + JOIN + APPROVAL + LOOP(FOR_EACH/WHILE).
Nested LOOP / body APPROVAL fail closed before MCP.

Authorable APPROVAL is an Execution-level Plan checkpoint: entering wait
clears the lease and may freeze unrelated READY sibling branches until
``EXECUTION_APPROVAL_RESUME``. Multi-Step ToolPolicy approval and MRTR
remain ``DAG_WAIT_UNSUPPORTED``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.approval.evidence import (
    assert_current_context_matches_request,
    is_authorable_step_context,
)
from app.approval.wait import ApprovalWaitService
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalPolicyStatus,
    ApprovalStatus,
    AuthorableStepType,
    ExecutionStatus,
    LoopMode,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
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
)
from app.execution.dag import (
    StopCause,
    ToolJoinDag,
    approval_structurally_eligible,
    cancel_unused_ready_tools,
    condition_structurally_eligible,
    count_tool_slots,
    evaluate_join_policy,
    has_intentional_skip_dependency,
    is_single_tool_execution,
    join_barrier_ready,
    loop_structurally_eligible,
    pick_stop_cause,
    skip_all_pending,
    tool_structurally_eligible,
    validate_tool_join_dag,
)
from app.execution.loop_reconcile import (
    active_iteration_no,
    assert_collection_pin_stable,
    assert_expanded_max_steps,
    assert_while_next_iteration_max_steps,
    evaluate_while_gate,
    fail_loop_known,
    iteration_complete,
    loop_timeout_exceeded,
    materialize_iteration,
    pin_collection_evidence,
    pin_while_control_evidence,
    resolve_foreach_collection,
    runtime_plan_step,
    stop_loop_owned_untouched_children,
    succeed_loop,
    succeed_while_loop,
)
from app.execution.loop_runtime import (
    LOOP_MAX_ITERATIONS_EXCEEDED,
    LOOP_TIMEOUT,
)
from app.execution.predicate_evaluator import RuntimePredicateEvaluator
from app.models.execution import Execution, ExecutionStep
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.schemas.execution_plan import (
    ApprovalStepConfigV1,
    ComplexToolStepConfigV1,
    ConditionStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    JoinStepConfigV1,
    LoopStepConfigV1,
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
            if prepared.reason == "WAITING_APPROVAL":
                # Authorable APPROVAL wait — durable, lease-free; no ToolRunner.
                return ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=(
                        prepared.ready_step_ids[0]
                        if prepared.ready_step_ids
                        else None
                    ),
                    attempt_id=None,
                    tool_call_id=None,
                    mcp_called=False,
                    terminal_status=StepStatus.WAITING_APPROVAL.value,
                    reason="WAITING_APPROVAL",
                    disposition=DISPOSITION_WAIT,
                )
            if prepared.reason == "WAITING_HANDOFF" and prepared.ready_step_ids:
                # Single-TOOL ToolPolicy wait path — existing runner semantics.
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
                # Durable wait or one-sided wait corruption. Authorable APPROVAL
                # wait is lease-free and must not invoke McpToolRunner.
                # ToolPolicy wait still handoffs to the runner. Do not require a
                # live Execution lease here; WAITING_* clears the lease.
                if waiting or execution.status in {
                    ExecutionStatus.WAITING_APPROVAL.value,
                    ExecutionStatus.WAITING_INPUT.value,
                }:
                    target = waiting[0] if waiting else next(
                        (
                            s
                            for s in steps
                            if s.step_type
                            in {
                                AuthorableStepType.TOOL.value,
                                AuthorableStepType.APPROVAL.value,
                            }
                        ),
                        None,
                    )
                    if target is not None:
                        if target.step_type == AuthorableStepType.APPROVAL.value:
                            return ProgressOutcome(
                                execution_complete=False,
                                promoted=False,
                                reason="WAITING_APPROVAL",
                                ready_step_ids=(target.id,),
                                execution_status=execution.status,
                            )
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

                by_key = {s.step_key: s for s in steps}

                # Duplicate mid-wave delivery: TOOL RUNNING with STARTED ToolCall
                # means a remote call is already owned/in-flight — do not start a
                # competing wave (and do not fail-closed while Phase C may run).
                running_tools = [
                    s
                    for s in steps
                    if s.step_type == AuthorableStepType.TOOL.value
                    and s.status == StepStatus.RUNNING.value
                ]
                if running_tools:
                    in_flight = False
                    for rt in running_tools:
                        attempts = await executions.list_attempts(rt.id)
                        for attempt in attempts:
                            if attempt.status != StepAttemptStatus.STARTED.value:
                                continue
                            tool_calls = await executions.list_tool_calls(attempt.id)
                            if any(
                                tc.normalized_status
                                == ToolCallNormalizedStatus.STARTED.value
                                for tc in tool_calls
                            ):
                                in_flight = True
                                break
                        if in_flight:
                            break
                    if in_flight:
                        return ProgressOutcome(
                            execution_complete=False,
                            promoted=False,
                            reason=_REASON_WAVE_IN_PROGRESS,
                        )

                # Immutable Plan ↔ Step lineage. Post-claim corruption must not
                # strand RUNNING + lease via an escaped AppError.
                try:
                    plan = assert_execution_plan_lineage(execution)
                    dag = validate_tool_join_dag(plan, steps)
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )

                single_tool = is_single_tool_execution(steps)
                if running_tools:
                    # Resume RUNNING TOOLs (no STARTED ToolCall) as this wave.
                    resume_ids = [
                        by_key[k].id
                        for k in dag.ordered_step_keys
                        if by_key[k].status == StepStatus.RUNNING.value
                        and by_key[k].step_type == AuthorableStepType.TOOL.value
                    ]
                    execution.heartbeat_at = now
                    execution.lock_version += 1
                    await session.flush()
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason="WAVE_READY",
                        ready_step_ids=tuple(resume_ids),
                        plan_order=dag.ordered_step_keys,
                        single_tool=single_tool,
                    )

                # Local CONDITION / APPROVAL / when / JOIN fixed point before
                # TOOL wave. Authorable APPROVAL may suspend at this boundary.
                local_stop = await self._local_reconcile_locked(
                    executions=executions,
                    execution=execution,
                    plan=plan,
                    dag=dag,
                    steps=steps,
                    by_key=by_key,
                    now=now,
                    session=session,
                )
                if local_stop is not None:
                    await session.flush()
                    return local_stop

                # Refresh after local mutations (LOOP may have materialised
                # iteration body instances — rebuild the scheduling DAG).
                steps = await executions.list_steps(execution.id)
                by_key = {s.step_key: s for s in steps}
                try:
                    dag = validate_tool_join_dag(plan, steps)
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )

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

    async def _local_reconcile_locked(
        self,
        *,
        executions: ExecutionRepository,
        execution: Execution,
        plan: ExecutionPlanV1,
        dag: ToolJoinDag,
        steps: list[ExecutionStep],
        by_key: dict[str, ExecutionStep],
        now: datetime,
        session: AsyncSession,
    ) -> ProgressOutcome | None:
        """Fixed-point local reconcile: prune → LOOP → CONDITION/APPROVAL/when/JOIN.

        Local CONDITION/JOIN/APPROVAL/LOOP/when work does not consume
        ``max_parallelism``. Predicate evaluation failures are mandatory-fatal.
        Authorable APPROVAL may return WAITING_APPROVAL (Execution-level
        suspension) after creating at most one PENDING ApprovalRequest.
        """
        evaluator = RuntimePredicateEvaluator()
        plan_by_id = {p.id: p for p in plan.steps}
        # Bound iterations by Step count (each Step terminalizes at most once).
        # Allow growth when LOOP materialises body instances mid-reconcile.
        for _ in range(max(1, max(len(dag.ordered_step_keys), len(steps)) * 8 + 8)):
            changed = False

            # 1) Conditionally prune non-JOIN descendants of intentional skips.
            # Iteration-local: body deps are instance keys, so skip in iter N
            # does not prune iter N+1.
            for key in list(dag.ordered_step_keys):
                step = by_key.get(key)
                if step is None or step.status != StepStatus.PENDING.value:
                    continue
                if step.step_type == AuthorableStepType.JOIN.value:
                    continue
                if step.step_type not in {
                    AuthorableStepType.TOOL.value,
                    AuthorableStepType.CONDITION.value,
                    AuthorableStepType.APPROVAL.value,
                    AuthorableStepType.LOOP.value,
                }:
                    continue
                if not has_intentional_skip_dependency(
                    step=step, dag=dag, by_key=by_key
                ):
                    continue
                locked = await executions.lock_step(step.id)
                if locked is None or locked.status != StepStatus.PENDING.value:
                    continue
                locked.status = StepStatus.SKIPPED.value
                locked.error_code = "UPSTREAM_CONDITION_SKIPPED"
                locked.error_message = (
                    "Upstream Step was intentionally skipped by conditional "
                    "control flow."
                )
                locked.condition_result = False
                locked.finished_at = now
                locked.lock_version += 1
                by_key[locked.step_key] = locked
                changed = True
                logger.info(
                    "orchestrator prune skip execution_id=%s step_key=%s",
                    execution.id,
                    locked.step_key,
                )

            # 1b) Flat FOR_EACH LOOP start / advance / complete.
            loop_stop = await self._reconcile_loop_steps_locked(
                executions=executions,
                execution=execution,
                plan=plan,
                dag=dag,
                by_key=by_key,
                plan_by_id=plan_by_id,
                evaluator=evaluator,
                now=now,
            )
            if loop_stop is not None:
                if loop_stop.reason == "LOOP_CHANGED":
                    # Refresh DAG after materialization.
                    steps_ref = await executions.list_steps(execution.id)
                    by_key.clear()
                    by_key.update({s.step_key: s for s in steps_ref})
                    try:
                        dag = validate_tool_join_dag(plan, steps_ref)
                    except AppError as exc:
                        return self._fail_closed_lineage_locked(
                            execution=execution,
                            by_key=by_key,
                            now=now,
                            error_code=exc.code,
                            error_message=exc.message,
                        )
                    changed = True
                else:
                    return loop_stop

            # 2) Evaluate eligible CONDITION Steps (after dependency barrier).
            for key in list(dag.ordered_step_keys):
                step = by_key.get(key)
                if step is None or not condition_structurally_eligible(
                    step=step, dag=dag, by_key=by_key
                ):
                    continue
                locked = await executions.lock_step(step.id)
                if locked is None or locked.status != StepStatus.PENDING.value:
                    continue
                try:
                    plan_step = runtime_plan_step(locked, plan_by_id)
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                # Re-validate exact immutable snapshot before Predicate eval.
                if plan_step.model_dump(mode="json") != locked.step_snapshot:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code="RESOURCE_CONFLICT",
                        error_message=(
                            f"CONDITION step_snapshot drift for "
                            f"{locked.step_key!r}."
                        ),
                    )
                try:
                    when_false = self._evaluate_when_gate(
                        evaluator=evaluator,
                        plan_step=plan_step,
                        execution=execution,
                        owning_step=locked,
                        steps=list(by_key.values()),
                        plan=plan,
                    )
                except AppError as exc:
                    return self._fail_closed_predicate(
                        execution=execution,
                        by_key=by_key,
                        step=locked,
                        now=now,
                        plan=plan,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                if when_false:
                    self._apply_when_false(locked, now=now)
                    by_key[locked.step_key] = locked
                    changed = True
                    continue

                try:
                    cfg = ConditionStepConfigV1.model_validate(plan_step.config)
                    result = evaluator.evaluate(
                        cfg.predicate,
                        execution=execution,
                        owning_step=locked,
                        steps=list(by_key.values()),
                        plan=plan,
                    )
                except AppError as exc:
                    return self._fail_closed_predicate(
                        execution=execution,
                        by_key=by_key,
                        step=locked,
                        now=now,
                        plan=plan,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                except Exception as exc:
                    return self._fail_closed_predicate(
                        execution=execution,
                        by_key=by_key,
                        step=locked,
                        now=now,
                        plan=plan,
                        error_code="PREDICATE_EVALUATION_FAILED",
                        error_message=str(exc),
                    )

                locked.status = StepStatus.READY.value
                locked.ready_at = now
                locked.status = StepStatus.RUNNING.value
                if locked.started_at is None:
                    locked.started_at = now
                locked.status = StepStatus.SUCCEEDED.value
                locked.condition_result = bool(result)
                locked.result_inline = {"condition_result": bool(result)}
                locked.error_code = None
                locked.error_message = None
                locked.finished_at = now
                locked.lock_version += 1
                by_key[locked.step_key] = locked
                changed = True
                logger.info(
                    "orchestrator CONDITION execution_id=%s step_key=%s result=%s",
                    execution.id,
                    locked.step_key,
                    result,
                )

            # 2b) Authorable APPROVAL: complete approved READY, else enter wait.
            approval_stop = await self._reconcile_approval_steps_locked(
                executions=executions,
                execution=execution,
                plan=plan,
                dag=dag,
                by_key=by_key,
                plan_by_id=plan_by_id,
                evaluator=evaluator,
                now=now,
                session=session,
            )
            if approval_stop is not None:
                if approval_stop.reason == "APPROVAL_CHANGED":
                    changed = True
                else:
                    return approval_stop

            # 3) Evaluate Step.when for PENDING TOOL with barrier satisfied.
            for key in list(dag.ordered_step_keys):
                step = by_key.get(key)
                if step is None or step.status != StepStatus.PENDING.value:
                    continue
                if step.step_type != AuthorableStepType.TOOL.value:
                    continue
                if not tool_structurally_eligible(
                    step=step, dag=dag, by_key=by_key
                ):
                    continue
                try:
                    plan_step = runtime_plan_step(step, plan_by_id)
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                if plan_step.when is None:
                    continue
                locked = await executions.lock_step(step.id)
                if locked is None or locked.status != StepStatus.PENDING.value:
                    continue
                if plan_step.model_dump(mode="json") != locked.step_snapshot:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code="RESOURCE_CONFLICT",
                        error_message=(
                            f"TOOL when step_snapshot drift for "
                            f"{locked.step_key!r}."
                        ),
                    )
                try:
                    when_false = self._evaluate_when_gate(
                        evaluator=evaluator,
                        plan_step=plan_step,
                        execution=execution,
                        owning_step=locked,
                        steps=list(by_key.values()),
                        plan=plan,
                    )
                except AppError as exc:
                    return self._fail_closed_predicate(
                        execution=execution,
                        by_key=by_key,
                        step=locked,
                        now=now,
                        plan=plan,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                if when_false:
                    self._apply_when_false(locked, now=now)
                    by_key[locked.step_key] = locked
                    changed = True
                elif locked.condition_result is not True:
                    # Gate evidence for non-CONDITION Steps (TOOL stays PENDING
                    # for wave reservation; do not force another reconcile loop).
                    locked.condition_result = True
                    locked.lock_version += 1
                    by_key[locked.step_key] = locked

            # 4) JOIN barrier: when gate then policy.
            for key in list(dag.ordered_step_keys):
                step = by_key.get(key)
                if step is None or not join_barrier_ready(
                    step=step, dag=dag, by_key=by_key
                ):
                    continue
                locked = await executions.lock_step(step.id)
                if locked is None or locked.status != StepStatus.PENDING.value:
                    continue
                try:
                    plan_step = runtime_plan_step(locked, plan_by_id)
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                if plan_step.model_dump(mode="json") != locked.step_snapshot:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code="RESOURCE_CONFLICT",
                        error_message=(
                            f"JOIN step_snapshot drift for {locked.step_key!r}."
                        ),
                    )
                try:
                    when_false = self._evaluate_when_gate(
                        evaluator=evaluator,
                        plan_step=plan_step,
                        execution=execution,
                        owning_step=locked,
                        steps=list(by_key.values()),
                        plan=plan,
                    )
                except AppError as exc:
                    return self._fail_closed_predicate(
                        execution=execution,
                        by_key=by_key,
                        step=locked,
                        now=now,
                        plan=plan,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                if when_false:
                    self._apply_when_false(locked, now=now)
                    by_key[locked.step_key] = locked
                    changed = True
                    continue
                if plan_step.when is not None:
                    locked.condition_result = True

                cfg = JoinStepConfigV1.model_validate(plan_step.config)
                dep_statuses = [
                    by_key[d].status for d in dag.dependencies[locked.step_key]
                ]
                terminal, error_code = evaluate_join_policy(
                    policy=cfg.policy, dependency_statuses=dep_statuses
                )
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
                changed = True
                logger.info(
                    "orchestrator JOIN terminal execution_id=%s step_key=%s status=%s",
                    execution.id,
                    locked.step_key,
                    terminal,
                )

                if terminal == StepStatus.SUCCEEDED.value:
                    continue
                on_error = plan_step.on_error
                if on_error == "FAIL_EXECUTION" or not is_continuable_known_failure(
                    on_error=on_error, step_status=terminal
                ):
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

            if not changed:
                return None
        return None

    async def _reconcile_loop_steps_locked(
        self,
        *,
        executions: ExecutionRepository,
        execution: Execution,
        plan: ExecutionPlanV1,
        dag: ToolJoinDag,
        by_key: dict[str, ExecutionStep],
        plan_by_id: dict[str, ExecutionPlanStep],
        evaluator: RuntimePredicateEvaluator,
        now: datetime,
    ) -> ProgressOutcome | None:
        """Start / advance / complete flat FOR_EACH / WHILE LOOP Steps.

        Returns:
        - ProgressOutcome reason=LOOP_CHANGED when local state advanced
        - ProgressOutcome fail-closed / ErrorPolicy stop
        - None when no LOOP work this pass
        """
        changed = False

        for key in list(dag.ordered_step_keys):
            step = by_key.get(key)
            if step is None:
                continue
            if step.step_type != AuthorableStepType.LOOP.value:
                continue
            if step.parent_step_id is not None:
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code="RESOURCE_CONFLICT",
                    error_message="Nested LOOP runtime row is unsupported.",
                )

            # PENDING → start
            if step.status == StepStatus.PENDING.value:
                if not loop_structurally_eligible(
                    step=step, dag=dag, by_key=by_key
                ):
                    continue
                locked = await executions.lock_step(step.id)
                if locked is None or locked.status != StepStatus.PENDING.value:
                    continue
                try:
                    plan_step = runtime_plan_step(locked, plan_by_id)
                    cfg = LoopStepConfigV1.model_validate(plan_step.config)
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                except Exception as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code="RESOURCE_CONFLICT",
                        error_message=f"Invalid LOOP config: {exc}",
                    )
                try:
                    when_false = self._evaluate_when_gate(
                        evaluator=evaluator,
                        plan_step=plan_step,
                        execution=execution,
                        owning_step=locked,
                        steps=list(by_key.values()),
                        plan=plan,
                    )
                except AppError as exc:
                    return self._fail_closed_predicate(
                        execution=execution,
                        by_key=by_key,
                        step=locked,
                        now=now,
                        plan=plan,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                if when_false:
                    self._apply_when_false(locked, now=now)
                    by_key[locked.step_key] = locked
                    changed = True
                    continue

                locked.status = StepStatus.READY.value
                locked.ready_at = now
                locked.status = StepStatus.RUNNING.value
                if locked.started_at is None:
                    locked.started_at = now
                locked.lock_version += 1

                if cfg.mode == LoopMode.WHILE:
                    try:
                        pin_while_control_evidence(loop_step=locked)
                        by_key[locked.step_key] = locked
                        if loop_timeout_exceeded(locked, now=now):
                            fail_loop_known(
                                locked,
                                status=StepStatus.TIMED_OUT.value,
                                error_code=LOOP_TIMEOUT,
                                error_message="LOOP.timeout_seconds budget exhausted.",
                                now=now,
                            )
                            by_key[locked.step_key] = locked
                            stop = self._apply_loop_error_policy(
                                execution=execution,
                                by_key=by_key,
                                loop_step=locked,
                                plan_step=plan_step,
                                plan=plan,
                                now=now,
                            )
                            if stop is not None:
                                return stop
                            changed = True
                            continue
                        gate = evaluate_while_gate(
                            evaluator=evaluator,
                            execution=execution,
                            loop_step=locked,
                            plan=plan,
                            cfg=cfg,
                            steps=list(by_key.values()),
                            candidate_iteration_no=1,
                        )
                        by_key[locked.step_key] = locked
                        if not gate:
                            succeed_while_loop(
                                locked, iterations_completed=0, now=now
                            )
                            by_key[locked.step_key] = locked
                            changed = True
                            continue
                        if 1 > cfg.max_iterations:
                            fail_loop_known(
                                locked,
                                status=StepStatus.FAILED.value,
                                error_code=LOOP_MAX_ITERATIONS_EXCEEDED,
                                error_message=(
                                    f"WHILE candidate 1 exceeds "
                                    f"max_iterations={cfg.max_iterations}."
                                ),
                                now=now,
                            )
                            by_key[locked.step_key] = locked
                            stop = self._apply_loop_error_policy(
                                execution=execution,
                                by_key=by_key,
                                loop_step=locked,
                                plan_step=plan_step,
                                plan=plan,
                                now=now,
                            )
                            if stop is not None:
                                return stop
                            changed = True
                            continue
                        assert_while_next_iteration_max_steps(
                            plan=plan,
                            steps=list(by_key.values()),
                            body_step_count=len(cfg.body_step_ids),
                        )
                        created = await materialize_iteration(
                            executions=executions,
                            execution=execution,
                            loop_step=locked,
                            plan=plan,
                            cfg=cfg,
                            iteration_no=1,
                            existing_steps=list(by_key.values()),
                        )
                        for row in created:
                            by_key[row.step_key] = row
                        by_key[locked.step_key] = locked
                        changed = True
                        logger.info(
                            "orchestrator WHILE start execution_id=%s "
                            "step_key=%s",
                            execution.id,
                            locked.step_key,
                        )
                        continue
                    except AppError as exc:
                        if exc.code.startswith("PREDICATE_"):
                            if locked.status == StepStatus.RUNNING.value:
                                locked.status = StepStatus.FAILED.value
                                locked.error_code = exc.code
                                locked.error_message = exc.message
                                locked.finished_at = now
                                locked.lock_version += 1
                                by_key[locked.step_key] = locked
                            return self._fail_closed_predicate(
                                execution=execution,
                                by_key=by_key,
                                step=locked,
                                now=now,
                                plan=plan,
                                error_code=exc.code,
                                error_message=exc.message,
                            )
                        if locked.status == StepStatus.RUNNING.value:
                            locked.status = StepStatus.FAILED.value
                            locked.error_code = exc.code
                            locked.error_message = exc.message
                            locked.finished_at = now
                            locked.lock_version += 1
                            by_key[locked.step_key] = locked
                        return self._fail_closed_lineage_locked(
                            execution=execution,
                            by_key=by_key,
                            now=now,
                            error_code=exc.code,
                            error_message=exc.message,
                        )

                try:
                    collection = resolve_foreach_collection(
                        execution=execution,
                        loop_step=locked,
                        plan=plan,
                        steps=list(by_key.values()),
                        cfg=cfg,
                    )
                    pin_collection_evidence(
                        loop_step=locked, collection=collection
                    )
                    # Include pinned RUNNING LOOP in by_key before global budget.
                    by_key[locked.step_key] = locked
                    if len(collection) > cfg.max_iterations:
                        fail_loop_known(
                            locked,
                            status=StepStatus.FAILED.value,
                            error_code=LOOP_MAX_ITERATIONS_EXCEEDED,
                            error_message=(
                                f"collection_size={len(collection)} exceeds "
                                f"max_iterations={cfg.max_iterations}."
                            ),
                            now=now,
                        )
                        by_key[locked.step_key] = locked
                        stop = self._apply_loop_error_policy(
                            execution=execution,
                            by_key=by_key,
                            loop_step=locked,
                            plan_step=plan_step,
                            plan=plan,
                            now=now,
                        )
                        if stop is not None:
                            return stop
                        changed = True
                        continue
                    # Global expanded max_steps: after pin, before iter-1 MCP.
                    assert_expanded_max_steps(
                        plan=plan,
                        steps=list(by_key.values()),
                    )
                except AppError as exc:
                    # Mandatory-fatal for collection type / pin / max_steps /
                    # binding integrity. Known max_iterations already handled.
                    if exc.code == LOOP_MAX_ITERATIONS_EXCEEDED:
                        fail_loop_known(
                            locked,
                            status=StepStatus.FAILED.value,
                            error_code=exc.code,
                            error_message=exc.message,
                            now=now,
                        )
                        by_key[locked.step_key] = locked
                        stop = self._apply_loop_error_policy(
                            execution=execution,
                            by_key=by_key,
                            loop_step=locked,
                            plan_step=plan_step,
                            plan=plan,
                            now=now,
                        )
                        if stop is not None:
                            return stop
                        changed = True
                        continue
                    if locked.status == StepStatus.RUNNING.value:
                        locked.status = StepStatus.FAILED.value
                        locked.error_code = exc.code
                        locked.error_message = exc.message
                        locked.finished_at = now
                        locked.lock_version += 1
                        by_key[locked.step_key] = locked
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )

                by_key[locked.step_key] = locked
                size = int((locked.resolved_input or {}).get("collection_size", 0))
                if size == 0:
                    succeed_loop(
                        locked,
                        collection_size=0,
                        iterations_completed=0,
                        now=now,
                    )
                    by_key[locked.step_key] = locked
                    changed = True
                    continue

                created = await materialize_iteration(
                    executions=executions,
                    execution=execution,
                    loop_step=locked,
                    plan=plan,
                    cfg=cfg,
                    iteration_no=1,
                    existing_steps=list(by_key.values()),
                )
                for row in created:
                    by_key[row.step_key] = row
                changed = True
                logger.info(
                    "orchestrator LOOP start execution_id=%s step_key=%s size=%s",
                    execution.id,
                    locked.step_key,
                    size,
                )
                continue

            # RUNNING → timeout / advance / complete
            if step.status != StepStatus.RUNNING.value:
                continue
            locked = await executions.lock_step(step.id)
            if locked is None or locked.status != StepStatus.RUNNING.value:
                continue
            try:
                plan_step = runtime_plan_step(locked, plan_by_id)
                cfg = LoopStepConfigV1.model_validate(plan_step.config)
            except AppError as exc:
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            except Exception as exc:
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code="RESOURCE_CONFLICT",
                    error_message=f"Invalid LOOP config: {exc}",
                )

            if loop_timeout_exceeded(locked, now=now):
                fail_loop_known(
                    locked,
                    status=StepStatus.TIMED_OUT.value,
                    error_code=LOOP_TIMEOUT,
                    error_message="LOOP.timeout_seconds budget exhausted.",
                    now=now,
                )
                by_key[locked.step_key] = locked
                stop = self._apply_loop_error_policy(
                    execution=execution,
                    by_key=by_key,
                    loop_step=locked,
                    plan_step=plan_step,
                    plan=plan,
                    now=now,
                )
                if stop is not None:
                    return stop
                changed = True
                continue

            if cfg.mode == LoopMode.WHILE:
                try:
                    current = active_iteration_no(
                        steps=list(by_key.values()), parent_step_id=locked.id
                    )
                    # Zero-iteration SUCCEEDED already handled at start.
                    # If RUNNING with no children yet, re-evaluate candidate 1
                    # (duplicate delivery after pin but before materialize).
                    if current < 1:
                        gate = evaluate_while_gate(
                            evaluator=evaluator,
                            execution=execution,
                            loop_step=locked,
                            plan=plan,
                            cfg=cfg,
                            steps=list(by_key.values()),
                            candidate_iteration_no=1,
                        )
                        by_key[locked.step_key] = locked
                        if not gate:
                            succeed_while_loop(
                                locked, iterations_completed=0, now=now
                            )
                            by_key[locked.step_key] = locked
                            changed = True
                            continue
                        if 1 > cfg.max_iterations:
                            fail_loop_known(
                                locked,
                                status=StepStatus.FAILED.value,
                                error_code=LOOP_MAX_ITERATIONS_EXCEEDED,
                                error_message=(
                                    f"WHILE candidate 1 exceeds "
                                    f"max_iterations={cfg.max_iterations}."
                                ),
                                now=now,
                            )
                            by_key[locked.step_key] = locked
                            stop = self._apply_loop_error_policy(
                                execution=execution,
                                by_key=by_key,
                                loop_step=locked,
                                plan_step=plan_step,
                                plan=plan,
                                now=now,
                            )
                            if stop is not None:
                                return stop
                            changed = True
                            continue
                        assert_while_next_iteration_max_steps(
                            plan=plan,
                            steps=list(by_key.values()),
                            body_step_count=len(cfg.body_step_ids),
                        )
                        created = await materialize_iteration(
                            executions=executions,
                            execution=execution,
                            loop_step=locked,
                            plan=plan,
                            cfg=cfg,
                            iteration_no=1,
                            existing_steps=list(by_key.values()),
                        )
                        for row in created:
                            by_key[row.step_key] = row
                        by_key[locked.step_key] = locked
                        changed = True
                        continue

                    if not iteration_complete(
                        steps=list(by_key.values()),
                        parent_step_id=locked.id,
                        iteration_no=current,
                        body_step_count=len(cfg.body_step_ids),
                    ):
                        continue

                    # Iteration settled — timeout before next Predicate.
                    if loop_timeout_exceeded(locked, now=now):
                        fail_loop_known(
                            locked,
                            status=StepStatus.TIMED_OUT.value,
                            error_code=LOOP_TIMEOUT,
                            error_message=(
                                "LOOP.timeout_seconds budget exhausted."
                            ),
                            now=now,
                        )
                        by_key[locked.step_key] = locked
                        stop = self._apply_loop_error_policy(
                            execution=execution,
                            by_key=by_key,
                            loop_step=locked,
                            plan_step=plan_step,
                            plan=plan,
                            now=now,
                        )
                        if stop is not None:
                            return stop
                        changed = True
                        continue

                    nxt = current + 1
                    gate = evaluate_while_gate(
                        evaluator=evaluator,
                        execution=execution,
                        loop_step=locked,
                        plan=plan,
                        cfg=cfg,
                        steps=list(by_key.values()),
                        candidate_iteration_no=nxt,
                    )
                    by_key[locked.step_key] = locked
                    if not gate:
                        succeed_while_loop(
                            locked,
                            iterations_completed=current,
                            now=now,
                        )
                        by_key[locked.step_key] = locked
                        changed = True
                        logger.info(
                            "orchestrator WHILE complete execution_id=%s "
                            "step_key=%s iters=%s",
                            execution.id,
                            locked.step_key,
                            current,
                        )
                        continue
                    # Predicate true — enforce max_iterations after eval.
                    if nxt > cfg.max_iterations:
                        fail_loop_known(
                            locked,
                            status=StepStatus.FAILED.value,
                            error_code=LOOP_MAX_ITERATIONS_EXCEEDED,
                            error_message=(
                                f"WHILE candidate {nxt} exceeds "
                                f"max_iterations={cfg.max_iterations}."
                            ),
                            now=now,
                        )
                        by_key[locked.step_key] = locked
                        stop = self._apply_loop_error_policy(
                            execution=execution,
                            by_key=by_key,
                            loop_step=locked,
                            plan_step=plan_step,
                            plan=plan,
                            now=now,
                        )
                        if stop is not None:
                            return stop
                        changed = True
                        continue
                    assert_while_next_iteration_max_steps(
                        plan=plan,
                        steps=list(by_key.values()),
                        body_step_count=len(cfg.body_step_ids),
                    )
                    created = await materialize_iteration(
                        executions=executions,
                        execution=execution,
                        loop_step=locked,
                        plan=plan,
                        cfg=cfg,
                        iteration_no=nxt,
                        existing_steps=list(by_key.values()),
                    )
                    for row in created:
                        by_key[row.step_key] = row
                    by_key[locked.step_key] = locked
                    changed = True
                    logger.info(
                        "orchestrator WHILE next iter execution_id=%s "
                        "step_key=%s iter=%s",
                        execution.id,
                        locked.step_key,
                        nxt,
                    )
                    continue
                except AppError as exc:
                    # Known LOOP failures already handled; integrity → fatal.
                    if exc.code.startswith("PREDICATE_"):
                        if locked.status == StepStatus.RUNNING.value:
                            locked.status = StepStatus.FAILED.value
                            locked.error_code = exc.code
                            locked.error_message = exc.message
                            locked.finished_at = now
                            locked.lock_version += 1
                            by_key[locked.step_key] = locked
                        return self._fail_closed_predicate(
                            execution=execution,
                            by_key=by_key,
                            step=locked,
                            now=now,
                            plan=plan,
                            error_code=exc.code,
                            error_message=exc.message,
                        )
                    if locked.status == StepStatus.RUNNING.value:
                        locked.status = StepStatus.FAILED.value
                        locked.error_code = exc.code
                        locked.error_message = exc.message
                        locked.finished_at = now
                        locked.lock_version += 1
                        by_key[locked.step_key] = locked
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )

            try:
                collection = resolve_foreach_collection(
                    execution=execution,
                    loop_step=locked,
                    plan=plan,
                    steps=list(by_key.values()),
                    cfg=cfg,
                )
                assert_collection_pin_stable(
                    loop_step=locked, collection=collection
                )
            except AppError as exc:
                locked.status = StepStatus.FAILED.value
                locked.error_code = exc.code
                locked.error_message = exc.message
                locked.finished_at = now
                locked.lock_version += 1
                by_key[locked.step_key] = locked
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code=exc.code,
                    error_message=exc.message,
                )

            collection_size = len(collection)
            current = active_iteration_no(
                steps=list(by_key.values()), parent_step_id=locked.id
            )
            if current < 1:
                # Should have been materialised at start; recover by creating 1.
                created = await materialize_iteration(
                    executions=executions,
                    execution=execution,
                    loop_step=locked,
                    plan=plan,
                    cfg=cfg,
                    iteration_no=1,
                    existing_steps=list(by_key.values()),
                )
                for row in created:
                    by_key[row.step_key] = row
                changed = True
                continue

            if not iteration_complete(
                steps=list(by_key.values()),
                parent_step_id=locked.id,
                iteration_no=current,
                body_step_count=len(cfg.body_step_ids),
            ):
                continue

            # Iteration settled — check timeout before next / finalize.
            if loop_timeout_exceeded(locked, now=now):
                fail_loop_known(
                    locked,
                    status=StepStatus.TIMED_OUT.value,
                    error_code=LOOP_TIMEOUT,
                    error_message="LOOP.timeout_seconds budget exhausted.",
                    now=now,
                )
                by_key[locked.step_key] = locked
                stop = self._apply_loop_error_policy(
                    execution=execution,
                    by_key=by_key,
                    loop_step=locked,
                    plan_step=plan_step,
                    plan=plan,
                    now=now,
                )
                if stop is not None:
                    return stop
                changed = True
                continue

            if current < collection_size:
                nxt = current + 1
                created = await materialize_iteration(
                    executions=executions,
                    execution=execution,
                    loop_step=locked,
                    plan=plan,
                    cfg=cfg,
                    iteration_no=nxt,
                    existing_steps=list(by_key.values()),
                )
                for row in created:
                    by_key[row.step_key] = row
                by_key[locked.step_key] = locked
                changed = True
                logger.info(
                    "orchestrator LOOP next iter execution_id=%s step_key=%s "
                    "iter=%s",
                    execution.id,
                    locked.step_key,
                    nxt,
                )
                continue

            succeed_loop(
                locked,
                collection_size=collection_size,
                iterations_completed=collection_size,
                now=now,
            )
            by_key[locked.step_key] = locked
            changed = True
            logger.info(
                "orchestrator LOOP complete execution_id=%s step_key=%s "
                "iters=%s",
                execution.id,
                locked.step_key,
                collection_size,
            )

        if changed:
            return ProgressOutcome(
                execution_complete=False,
                promoted=False,
                reason="LOOP_CHANGED",
            )
        return None

    def _apply_loop_error_policy(
        self,
        *,
        execution: Execution,
        by_key: dict[str, ExecutionStep],
        loop_step: ExecutionStep,
        plan_step: ExecutionPlanStep,
        plan: ExecutionPlanV1,
        now: datetime,
    ) -> ProgressOutcome | None:
        """Apply LOOP on_error for known failures (max_iterations / timeout).

        Always terminalize untouched children of this LOOP first so CONTINUE /
        MARK_PARTIAL cannot strand PENDING body rows while the parent is
        terminal (``_dag_naturally_ended`` deadlock).
        """
        try:
            stop_loop_owned_untouched_children(
                by_key=by_key, loop_step=loop_step, now=now
            )
        except AppError as exc:
            return self._fail_closed_lineage_locked(
                execution=execution,
                by_key=by_key,
                now=now,
                error_code=exc.code,
                error_message=exc.message,
            )
        on_error = plan_step.on_error
        terminal = loop_step.status
        if on_error == "FAIL_EXECUTION" or not is_continuable_known_failure(
            on_error=on_error, step_status=terminal
        ):
            skip_all_pending(by_key=by_key, now=now)
            cancel_unused_ready_tools(by_key=by_key, now=now)
            exec_status = fail_fast_execution_status(terminal)
            execution.status = exec_status
            execution.error_code = loop_step.error_code
            execution.error_message = loop_step.error_message
            execution.result_summary = build_result_summary(
                status=exec_status, steps=list(by_key.values()), plan=plan
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
        # CONTINUE / MARK_PARTIAL — LOOP terminal + children cleaned; downstream
        # may proceed. Parent LOOP terminal status is the failure evidence.
        return None

    async def _reconcile_approval_steps_locked(
        self,
        *,
        executions: ExecutionRepository,
        execution: Execution,
        plan: ExecutionPlanV1,
        dag: ToolJoinDag,
        by_key: dict[str, ExecutionStep],
        plan_by_id: dict[str, ExecutionPlanStep],
        evaluator: RuntimePredicateEvaluator,
        now: datetime,
        session: AsyncSession,
    ) -> ProgressOutcome | None:
        """Complete approved READY APPROVAL or enter one authorable wait.

        Returns:
        - ProgressOutcome WAITING_APPROVAL when entering durable wait
        - ProgressOutcome reason=APPROVAL_CHANGED when local SUCCEEDED/SKIPPED
        - ProgressOutcome fail-closed on lineage/policy errors
        - None when no APPROVAL work this iteration
        """
        requests = ApprovalRequestRepository(session)
        policies = ApprovalPolicyRepository(session)
        wait_service = ApprovalWaitService(session)
        changed = False

        # Complete READY APPROVAL Steps that already hold APPROVED evidence
        # (post-resume). Never create a new request for an already-approved Step.
        for key in dag.ordered_step_keys:
            step = by_key[key]
            if (
                step.step_type != AuthorableStepType.APPROVAL.value
                or step.status != StepStatus.READY.value
            ):
                continue
            locked = await executions.lock_step(step.id)
            if locked is None or locked.status != StepStatus.READY.value:
                continue
            plan_step = plan_by_id[locked.step_key]
            if plan_step.model_dump(mode="json") != locked.step_snapshot:
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code="RESOURCE_CONFLICT",
                    error_message=(
                        f"APPROVAL step_snapshot drift for {locked.step_key!r}."
                    ),
                )
            approved_rows = await requests.find_approved_for_step(
                execution_id=execution.id, step_execution_id=locked.id
            )
            approved = next(
                (
                    r
                    for r in approved_rows
                    if r.resolved_at is not None
                    and is_authorable_step_context(r.context_snapshot)
                ),
                None,
            )
            if approved is None:
                # READY without APPROVED evidence is unexpected for authorable
                # APPROVAL (resume always pairs them). Fail closed.
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code="APPROVAL_RESUME_PRECONDITION_FAILED",
                    error_message=(
                        f"APPROVAL Step {locked.step_key!r} is READY without "
                        "valid APPROVED ApprovalRequest evidence."
                    ),
                )
            try:
                await assert_current_context_matches_request(
                    session,
                    request=approved,
                    execution=execution,
                    step=locked,
                    steps=list(by_key.values()),
                    plan=plan,
                )
            except AppError as exc:
                locked.status = StepStatus.FAILED.value
                locked.error_code = "APPROVAL_RESUME_PRECONDITION_FAILED"
                locked.error_message = exc.message
                locked.finished_at = now
                locked.lock_version += 1
                by_key[locked.step_key] = locked
                skip_all_pending(by_key=by_key, now=now)
                cancel_unused_ready_tools(by_key=by_key, now=now)
                execution.status = ExecutionStatus.FAILED.value
                execution.error_code = "APPROVAL_RESUME_PRECONDITION_FAILED"
                execution.error_message = exc.message
                execution.result_summary = build_result_summary(
                    status=ExecutionStatus.FAILED.value,
                    steps=list(by_key.values()),
                    plan=plan,
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
                    reason=_REASON_EXECUTION_FAILED,
                    execution_status=ExecutionStatus.FAILED.value,
                    step_terminal_status=StepStatus.FAILED.value,
                )

            locked.status = StepStatus.RUNNING.value
            if locked.started_at is None:
                locked.started_at = now
            locked.status = StepStatus.SUCCEEDED.value
            locked.result_inline = {
                "approval_status": ApprovalStatus.APPROVED.value,
                "approval_request_id": str(approved.id),
            }
            # Preserve prior when-gate evidence (true) or leave null when absent.
            locked.error_code = None
            locked.error_message = None
            locked.finished_at = now
            locked.lock_version += 1
            by_key[locked.step_key] = locked
            changed = True
            logger.info(
                "orchestrator APPROVAL succeeded execution_id=%s step_key=%s "
                "approval_request_id=%s",
                execution.id,
                locked.step_key,
                approved.id,
            )

        # Serialize: at most one PENDING ApprovalRequest per Execution.
        existing_pending = await requests.find_pending_for_execution(
            execution_id=execution.id
        )
        if existing_pending is not None:
            if changed:
                return ProgressOutcome(
                    execution_complete=False,
                    promoted=False,
                    reason="APPROVAL_CHANGED",
                )
            return None

        # Enter wait for the first eligible PENDING APPROVAL in Plan order.
        for key in dag.ordered_step_keys:
            step = by_key[key]
            if not approval_structurally_eligible(
                step=step, dag=dag, by_key=by_key
            ):
                continue
            locked = await executions.lock_step(step.id)
            if locked is None or locked.status != StepStatus.PENDING.value:
                continue
            plan_step = plan_by_id[locked.step_key]
            if plan_step.model_dump(mode="json") != locked.step_snapshot:
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code="RESOURCE_CONFLICT",
                    error_message=(
                        f"APPROVAL step_snapshot drift for {locked.step_key!r}."
                    ),
                )
            try:
                when_false = self._evaluate_when_gate(
                    evaluator=evaluator,
                    plan_step=plan_step,
                    execution=execution,
                    owning_step=locked,
                    steps=list(by_key.values()),
                    plan=plan,
                )
            except AppError as exc:
                return self._fail_closed_predicate(
                    execution=execution,
                    by_key=by_key,
                    step=locked,
                    now=now,
                    plan=plan,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            if when_false:
                self._apply_when_false(locked, now=now)
                by_key[locked.step_key] = locked
                changed = True
                continue
            if plan_step.when is not None:
                locked.condition_result = True

            try:
                cfg = ApprovalStepConfigV1.model_validate(plan_step.config)
            except Exception as exc:
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code="RESOURCE_CONFLICT",
                    error_message=(
                        f"Invalid APPROVAL config for {locked.step_key!r}: {exc}"
                    ),
                )
            policy = await policies.get(cfg.approval_policy_id)
            if policy is None or policy.status != ApprovalPolicyStatus.ACTIVE.value:
                return self._fail_closed_lineage_locked(
                    execution=execution,
                    by_key=by_key,
                    now=now,
                    error_code="EXECUTION_PRECONDITION_FAILED",
                    error_message=(
                        f"APPROVAL Step {locked.step_key!r} requires an ACTIVE "
                        "ApprovalPolicy."
                    ),
                )
            # Re-check serialization under Execution row lock (caller holds it).
            race_pending = await requests.find_pending_for_execution(
                execution_id=execution.id
            )
            if race_pending is not None:
                break

            outcome = await wait_service.enter_for_approval_step(
                execution=execution,
                step=locked,
                plan=plan,
                steps=list(by_key.values()),
                approval_policy=policy,
                now=now,
            )
            by_key[locked.step_key] = locked
            logger.info(
                "orchestrator APPROVAL wait execution_id=%s step_key=%s "
                "approval_request_id=%s reused=%s",
                execution.id,
                locked.step_key,
                outcome.approval_request_id,
                outcome.reused_existing,
            )
            return ProgressOutcome(
                execution_complete=False,
                promoted=False,
                reason="WAITING_APPROVAL",
                ready_step_ids=(locked.id,),
                execution_status=ExecutionStatus.WAITING_APPROVAL.value,
            )

        if changed:
            return ProgressOutcome(
                execution_complete=False,
                promoted=False,
                reason="APPROVAL_CHANGED",
            )
        return None

    @staticmethod
    def _evaluate_when_gate(
        *,
        evaluator: RuntimePredicateEvaluator,
        plan_step: ExecutionPlanStep,
        execution: Execution,
        owning_step: ExecutionStep,
        steps: list[ExecutionStep],
        plan: ExecutionPlanV1,
    ) -> bool:
        """Return True when ``when`` evaluates false (intentional skip).

        Absent ``when`` → False (proceed). Evaluation errors raise AppError.
        """
        if plan_step.when is None:
            return False
        result = evaluator.evaluate(
            plan_step.when,
            execution=execution,
            owning_step=owning_step,
            steps=steps,
            plan=plan,
        )
        return not bool(result)

    @staticmethod
    def _apply_when_false(step: ExecutionStep, *, now: datetime) -> None:
        step.status = StepStatus.SKIPPED.value
        step.error_code = "STEP_WHEN_FALSE"
        step.error_message = "Step.when evaluated to false; Step intentionally skipped."
        step.condition_result = False
        step.finished_at = now
        step.lock_version += 1

    def _fail_closed_predicate(
        self,
        *,
        execution: Execution,
        by_key: dict[str, ExecutionStep],
        step: ExecutionStep,
        now: datetime,
        plan: ExecutionPlanV1,
        error_code: str,
        error_message: str,
    ) -> ProgressOutcome:
        """Mandatory-fatal Predicate failure: active Step FAILED, Execution FAILED."""
        if step.status == StepStatus.PENDING.value:
            step.status = StepStatus.FAILED.value
            step.error_code = error_code
            step.error_message = error_message
            step.finished_at = now
            step.lock_version += 1
            by_key[step.step_key] = step
        skip_all_pending(by_key=by_key, now=now)
        cancel_unused_ready_tools(by_key=by_key, now=now)
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = error_code
        execution.error_message = error_message
        execution.result_summary = build_result_summary(
            status=ExecutionStatus.FAILED.value,
            steps=list(by_key.values()),
            plan=plan,
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
            reason=_REASON_EXECUTION_FAILED,
            execution_status=ExecutionStatus.FAILED.value,
            step_terminal_status=StepStatus.FAILED.value,
        )

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

                # Immutable Plan on_error / DAG lineage before ErrorPolicy.
                plan: ExecutionPlanV1 | None
                dag: ToolJoinDag | None
                try:
                    plan = assert_execution_plan_lineage(execution)
                    dag = validate_tool_join_dag(plan, steps)
                    plan_by_id = {p.id: p for p in plan.steps}
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=(
                            "Execution plan lineage corrupt after wave settlement."
                        ),
                    )

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
                    # Body instances use synthetic step_keys — resolve via
                    # step_snapshot / Plan template (not by_key == plan id).
                    try:
                        plan_step = runtime_plan_step(step, plan_by_id)
                        on_error = plan_step.on_error
                    except AppError:
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

                # Continuable wave — local CONDITION/APPROVAL/when/JOIN then maybe complete.
                local_stop = await self._local_reconcile_locked(
                    executions=executions,
                    execution=execution,
                    plan=plan,
                    dag=dag,
                    steps=steps,
                    by_key=by_key,
                    now=now,
                    session=session,
                )
                if local_stop is not None:
                    await session.flush()
                    return local_stop

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
                by_key = {s.step_key: s for s in steps}
                try:
                    plan = assert_execution_plan_lineage(execution)
                    dag = validate_tool_join_dag(plan, steps)
                except AppError as exc:
                    return self._fail_closed_lineage_locked(
                        execution=execution,
                        by_key=by_key,
                        now=now,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                local_stop = await self._local_reconcile_locked(
                    executions=executions,
                    execution=execution,
                    plan=plan,
                    dag=dag,
                    steps=steps,
                    by_key=by_key,
                    now=now,
                    session=session,
                )
                if local_stop is not None:
                    await session.flush()
                    return local_stop
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
    def _fail_closed_lineage_locked(
        *,
        execution: Execution,
        by_key: dict[str, ExecutionStep],
        now: datetime,
        error_code: str,
        error_message: str,
    ) -> ProgressOutcome:
        """Terminalize Execution on Plan/DAG lineage corruption without Plan parse.

        Does not overwrite terminal Step/Attempt/ToolCall evidence. Does not
        schedule new MCP work. Clears worker/lease/heartbeat even when Plan
        re-parse would fail.
        """
        skip_all_pending(by_key=by_key, now=now)
        cancel_unused_ready_tools(by_key=by_key, now=now)
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = error_code or "RESOURCE_CONFLICT"
        execution.error_message = error_message
        # Plan may be corrupt — summary without requiring Plan parse.
        execution.result_summary = build_result_summary(
            status=ExecutionStatus.FAILED.value,
            steps=list(by_key.values()),
            plan=None,
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
            reason=_REASON_EXECUTION_FAILED,
            execution_status=ExecutionStatus.FAILED.value,
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
