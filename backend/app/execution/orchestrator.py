"""Sequential TOOL-chain Execution orchestration (docs/04/05).

``McpToolRunner`` owns one TOOL Step invocation.
``ExecutionOrchestrator`` owns graph progression and Execution completion.

This slice supports only a linear TOOL-only DAG (exactly one root, no fan-out /
fan-in). CONDITION / JOIN / LOOP / parallel / STEP_OUTPUT resolution are out of
scope.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.enums import AuthorableStepType, ExecutionStatus, StepStatus
from app.models.execution import Execution, ExecutionStep
from app.repositories.execution import ExecutionRepository
from app.schemas.execution_plan import (
    ComplexToolStepConfigV1,
    ExecutionPlanStep,
    ExecutionPlanV1,
    compute_plan_hash,
)

logger = logging.getLogger(__name__)

_REASON_SAFE_RETRY_READY = "SAFE_RETRY_READY"
_REASON_WAITING_INPUT = "WAITING_INPUT"
_REASON_STEP_SUCCEEDED = "STEP_SUCCEEDED"
_REASON_EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"

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
    """Validated linear TOOL chain (root → … → leaf)."""

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

    # Fan-out: at most one dependent per upstream.
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


class ExecutionOrchestrator:
    """Claim-time root READY + post-success progression for sequential TOOL chains."""

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
        """Run the claimed sequential chain under one Execution lease."""
        from app.execution.tool_runner import ToolRunOutcome

        last_outcome: ToolRunOutcome | None = None
        while True:
            step_id = await self._select_ready_tool_step(
                execution_id=execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
            if step_id is None:
                # Durable wait states clear the lease — route to the Step-scoped
                # runner so existing WAITING_APPROVAL / WAITING_INPUT noop and
                # corruption checks remain authoritative.
                waiting_step_id = await self._find_waiting_step_id(execution_id)
                if waiting_step_id is not None:
                    return await self._runner.run_claimed_tool_step(
                        execution_id=execution_id,
                        step_execution_id=waiting_step_id,
                        worker_id=worker_id,
                        lease_token=lease_token,
                    )

                # No READY TOOL — either waiting/terminal, or need final SUCCEEDED.
                finalized = await self._finalize_if_all_succeeded(
                    execution_id=execution_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
                if last_outcome is not None:
                    if finalized:
                        return ToolRunOutcome(
                            execution_id=last_outcome.execution_id,
                            step_execution_id=last_outcome.step_execution_id,
                            attempt_id=last_outcome.attempt_id,
                            tool_call_id=last_outcome.tool_call_id,
                            mcp_called=last_outcome.mcp_called,
                            terminal_status=StepStatus.SUCCEEDED.value,
                            reason=_REASON_EXECUTION_SUCCEEDED,
                        )
                    return last_outcome
                # Idempotent duplicate delivery after terminalization / lost lease.
                async with self._session_factory() as session:
                    execution = await ExecutionRepository(session).get(execution_id)
                    steps = (
                        await ExecutionRepository(session).list_steps(execution_id)
                        if execution is not None
                        else []
                    )
                if execution is None:
                    return ToolRunOutcome(
                        execution_id=execution_id,
                        step_execution_id=None,
                        attempt_id=None,
                        tool_call_id=None,
                        mcp_called=False,
                        terminal_status=None,
                        reason="MISSING",
                    )
                if execution.status == ExecutionStatus.SUCCEEDED.value or (
                    steps and all(s.status == StepStatus.SUCCEEDED.value for s in steps)
                ):
                    return ToolRunOutcome(
                        execution_id=execution_id,
                        step_execution_id=steps[0].id if steps else None,
                        attempt_id=None,
                        tool_call_id=None,
                        mcp_called=False,
                        terminal_status=execution.status,
                        reason="STEP_ALREADY_TERMINAL",
                    )
                return ToolRunOutcome(
                    execution_id=execution_id,
                    step_execution_id=None,
                    attempt_id=None,
                    tool_call_id=None,
                    mcp_called=False,
                    terminal_status=execution.status,
                    reason="LEASE_MISMATCH",
                )

            outcome = await self._runner.run_claimed_tool_step(
                execution_id=execution_id,
                step_execution_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
            last_outcome = outcome

            if outcome.reason == _REASON_SAFE_RETRY_READY:
                continue
            if outcome.reason in {
                "WAITING_APPROVAL",
                _REASON_WAITING_INPUT,
                "LEASE_MISMATCH",
                "MISSING",
                "STEP_ALREADY_TERMINAL",
                "RESOURCE_CONFLICT",
            }:
                return outcome
            if outcome.terminal_status != StepStatus.SUCCEEDED.value:
                # Failure/timeout/unknown already terminalized Execution in runner.
                return outcome

            # Step SUCCEEDED while Execution stays RUNNING — promote next or finish.
            progressed = await self._promote_after_success(
                execution_id=execution_id,
                completed_step_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
            if progressed.reason in {"STALE_LEASE", "MISSING"}:
                return ToolRunOutcome(
                    execution_id=outcome.execution_id,
                    step_execution_id=outcome.step_execution_id,
                    attempt_id=outcome.attempt_id,
                    tool_call_id=outcome.tool_call_id,
                    mcp_called=outcome.mcp_called,
                    terminal_status=None,
                    reason="LEASE_MISMATCH"
                    if progressed.reason == "STALE_LEASE"
                    else "MISSING",
                )
            if progressed.execution_complete:
                return ToolRunOutcome(
                    execution_id=outcome.execution_id,
                    step_execution_id=outcome.step_execution_id,
                    attempt_id=outcome.attempt_id,
                    tool_call_id=outcome.tool_call_id,
                    mcp_called=outcome.mcp_called,
                    terminal_status=StepStatus.SUCCEEDED.value,
                    reason=_REASON_EXECUTION_SUCCEEDED,
                )
            # PROMOTED / ALREADY_READY — continue under same lease.
            continue

    async def _find_waiting_step_id(
        self, execution_id: uuid.UUID
    ) -> uuid.UUID | None:
        async with self._session_factory() as session:
            execution = await ExecutionRepository(session).get(execution_id)
            if execution is None:
                return None
            steps = await ExecutionRepository(session).list_steps(execution_id)
            if execution.status == ExecutionStatus.WAITING_APPROVAL.value:
                matching = [
                    s for s in steps if s.status == StepStatus.WAITING_APPROVAL.value
                ]
                if len(matching) == 1:
                    return matching[0].id
                if steps:
                    # One-sided corruption: still hand the sole/first Step to the
                    # runner so RESOURCE_CONFLICT semantics stay centralized.
                    return steps[0].id
                return None
            if execution.status == ExecutionStatus.WAITING_INPUT.value:
                matching = [
                    s for s in steps if s.status == StepStatus.WAITING_INPUT.value
                ]
                if len(matching) == 1:
                    return matching[0].id
                if steps:
                    return steps[0].id
                return None
            # One-sided Step wait while Execution is still RUNNING/other —
            # let the runner classify RESOURCE_CONFLICT.
            sided = [
                s
                for s in steps
                if s.status
                in {
                    StepStatus.WAITING_APPROVAL.value,
                    StepStatus.WAITING_INPUT.value,
                }
            ]
            if sided:
                return sided[0].id
            return None

    async def _select_ready_tool_step(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
    ) -> uuid.UUID | None:
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(execution_id)
                if execution is None:
                    return None
                now = datetime.now(UTC)
                expires = execution.lease_expires_at
                if expires is not None and expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                elif expires is not None:
                    expires = expires.astimezone(UTC)
                if (
                    execution.status != ExecutionStatus.RUNNING.value
                    or execution.worker_id != worker_id
                    or execution.lease_token != lease_token
                    or expires is None
                    or expires <= now
                ):
                    return None
                steps = await executions.list_steps(execution.id)
                ready = [
                    s
                    for s in steps
                    if s.status == StepStatus.READY.value
                    and s.step_type == AuthorableStepType.TOOL.value
                ]
                running = [
                    s
                    for s in steps
                    if s.status == StepStatus.RUNNING.value
                    and s.step_type == AuthorableStepType.TOOL.value
                ]
                # Prefer READY; allow exactly one RUNNING TOOL for Attempt/MRTR
                # resume under the same lease (not a second parallel Step).
                if ready:
                    if len(ready) != 1:
                        raise AppError(
                            code="RESOURCE_CONFLICT",
                            message=(
                                "Sequential orchestration allows exactly one READY "
                                f"TOOL Step; found {len(ready)}."
                            ),
                            status_code=409,
                        )
                    return ready[0].id
                if running:
                    if len(running) != 1:
                        raise AppError(
                            code="RESOURCE_CONFLICT",
                            message=(
                                "Sequential orchestration allows exactly one RUNNING "
                                f"TOOL Step; found {len(running)}."
                            ),
                            status_code=409,
                        )
                    return running[0].id
                return None

    async def _promote_after_success(
        self,
        *,
        execution_id: uuid.UUID,
        completed_step_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
    ) -> ProgressOutcome:
        """Promote the next PENDING TOOL or complete the Execution.

        Concurrent duplicate progression is serialized on the Execution row
        (``FOR UPDATE``). The loser observes READY/terminal state and returns a
        deterministic no-op reason without mutating lease or creating Steps.
        """
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(execution_id)
                if execution is None:
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason="MISSING",
                    )
                now = datetime.now(UTC)
                if not self._has_running_lease(
                    execution, worker_id, lease_token, now
                ):
                    # Stale/duplicate loser — never clear the winner's lease.
                    return ProgressOutcome(
                        execution_complete=execution.status
                        == ExecutionStatus.SUCCEEDED.value,
                        promoted=False,
                        reason="STALE_LEASE",
                    )
                steps = await executions.list_steps(execution.id)
                completed = next(
                    (s for s in steps if s.id == completed_step_id), None
                )
                if completed is None or completed.status != StepStatus.SUCCEEDED.value:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Completed Step is not SUCCEEDED during progression.",
                        status_code=409,
                    )

                plan = assert_execution_plan_lineage(execution)
                chain = validate_sequential_tool_chain(plan, steps)
                by_key = {s.step_key: s for s in steps}

                # Idempotent: if a later READY already exists, do not re-ready.
                already_ready = [
                    s
                    for s in steps
                    if s.status == StepStatus.READY.value
                    and s.step_type == AuthorableStepType.TOOL.value
                ]
                if already_ready:
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason="ALREADY_READY",
                    )

                # Find next PENDING whose single dependency is SUCCEEDED.
                next_key: str | None = None
                for index, key in enumerate(chain.ordered_step_keys):
                    if key != completed.step_key:
                        continue
                    if index + 1 < len(chain.ordered_step_keys):
                        next_key = chain.ordered_step_keys[index + 1]
                    break

                if next_key is None:
                    # Last step — complete Execution (idempotent if already done).
                    if not all(
                        by_key[k].status == StepStatus.SUCCEEDED.value
                        for k in chain.ordered_step_keys
                    ):
                        raise AppError(
                            code="RESOURCE_CONFLICT",
                            message="Cannot SUCCEEDED Execution with unfinished Steps.",
                            status_code=409,
                        )
                    if execution.status == ExecutionStatus.SUCCEEDED.value:
                        return ProgressOutcome(
                            execution_complete=True,
                            promoted=False,
                            reason="ALREADY_COMPLETE",
                        )
                    self._complete_execution_succeeded(execution, steps, now)
                    await session.flush()
                    return ProgressOutcome(
                        execution_complete=True,
                        promoted=False,
                        reason="EXECUTION_SUCCEEDED",
                    )

                nxt = by_key[next_key]
                # Re-lock the candidate Step so concurrent promoters serialize
                # on both Execution and the next Step row.
                locked_next = await executions.lock_step(nxt.id)
                if locked_next is None or locked_next.execution_id != execution.id:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Next Step missing during progression.",
                        status_code=409,
                    )
                if locked_next.status == StepStatus.READY.value:
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason="ALREADY_READY",
                    )
                if locked_next.status == StepStatus.SUCCEEDED.value:
                    if all(
                        by_key[k].status == StepStatus.SUCCEEDED.value
                        for k in chain.ordered_step_keys
                    ):
                        if execution.status == ExecutionStatus.RUNNING.value:
                            self._complete_execution_succeeded(execution, steps, now)
                            await session.flush()
                            return ProgressOutcome(
                                execution_complete=True,
                                promoted=False,
                                reason="EXECUTION_SUCCEEDED",
                            )
                        return ProgressOutcome(
                            execution_complete=True,
                            promoted=False,
                            reason="ALREADY_COMPLETE",
                        )
                    return ProgressOutcome(
                        execution_complete=False,
                        promoted=False,
                        reason="ALREADY_READY",
                    )
                if locked_next.status != StepStatus.PENDING.value:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            f"Next Step {next_key!r} is {locked_next.status!r}, "
                            "expected PENDING."
                        ),
                        status_code=409,
                    )

                plan_step = ExecutionPlanStep.model_validate(locked_next.step_snapshot)
                if plan_step.depends_on != [completed.step_key]:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Next Step dependency does not match completed Step.",
                        status_code=409,
                    )

                locked_next.status = StepStatus.READY.value
                locked_next.ready_at = now
                locked_next.lock_version += 1
                execution.heartbeat_at = now
                execution.lock_version += 1
                await session.flush()
                logger.info(
                    "orchestrator promoted step execution_id=%s step_key=%s",
                    execution_id,
                    next_key,
                )
                return ProgressOutcome(
                    execution_complete=False,
                    promoted=True,
                    reason="PROMOTED",
                )

    async def _finalize_if_all_succeeded(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(execution_id)
                if execution is None:
                    return False
                if execution.status == ExecutionStatus.SUCCEEDED.value:
                    return True
                now = datetime.now(UTC)
                if (
                    execution.status != ExecutionStatus.RUNNING.value
                    or execution.worker_id != worker_id
                    or execution.lease_token != lease_token
                ):
                    return False
                steps = await executions.list_steps(execution.id)
                if not steps:
                    return False
                if not all(s.status == StepStatus.SUCCEEDED.value for s in steps):
                    return False
                plan = assert_execution_plan_lineage(execution)
                validate_sequential_tool_chain(plan, steps)
                self._complete_execution_succeeded(execution, steps, now)
                await session.flush()
                return True

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
    def _complete_execution_succeeded(
        execution: Execution,
        steps: list[ExecutionStep],
        now: datetime,
    ) -> None:
        execution.status = ExecutionStatus.SUCCEEDED.value
        execution.error_code = None
        execution.error_message = None
        execution.result_summary = {
            "status": ExecutionStatus.SUCCEEDED.value,
            "step_keys": [s.step_key for s in steps],
            "step_count": len(steps),
        }
        execution.finished_at = now
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.lock_version += 1


@dataclass(frozen=True, slots=True)
class ProgressOutcome:
    """Result of sequential progression after a TOOL Step SUCCEEDED."""

    execution_complete: bool
    promoted: bool
    reason: str
