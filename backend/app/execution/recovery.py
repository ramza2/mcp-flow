"""Expired RUNNING Execution lease recovery (docs/02 FNC-EXE-011).

PostgreSQL is the source of truth. Celery/Redis only deliver recovery work.

This vertical slice covers AgentRequest + single TOOL Step + synchronous
Current MCP ``tools/call`` persisted evidence only. MRTR WAITING_INPUT,
Approval WAITING_APPROVAL, and MCP task-handle recovery are out of scope.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import _as_utc, _normalize_worker_id
from app.execution.lineage import assert_resume_attempt_lineage
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.execution_plan import DETERMINISTIC_TOOL_STEP_ID

logger = logging.getLogger(__name__)

_SAFE_RETRY_RISKS = frozenset(
    {RiskClass.READ_ONLY.value, RiskClass.IDEMPOTENT_WRITE.value}
)
_UNSAFE_RISKS = frozenset(
    {
        RiskClass.NON_IDEMPOTENT_WRITE.value,
        RiskClass.DESTRUCTIVE.value,
        RiskClass.UNKNOWN.value,
    }
)

_ATTEMPT_TERMINAL = frozenset(
    {
        StepAttemptStatus.SUCCEEDED.value,
        StepAttemptStatus.FAILED.value,
        StepAttemptStatus.TIMED_OUT.value,
        StepAttemptStatus.CANCELLED.value,
        StepAttemptStatus.UNKNOWN_OUTCOME.value,
    }
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

_ERROR_CODE_LEASE_EXPIRED = "WORKER_LEASE_EXPIRED"
_ERROR_CODE_INCONSISTENT = "RECOVERY_INCONSISTENT_EVIDENCE"
_ERROR_LAYER = "WORKER"


class RecoveryDecision(StrEnum):
    NO_OP = "NO_OP"
    TAKEOVER_READY = "TAKEOVER_READY"
    RESUME_ATTEMPT = "RESUME_ATTEMPT"
    SAFE_RETRY = "SAFE_RETRY"
    FAIL_EXHAUSTED = "FAIL_EXHAUSTED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    FAIL_INCONSISTENT = "FAIL_INCONSISTENT"


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    execution_id: uuid.UUID
    decision: RecoveryDecision
    taken_over: bool
    invoke_runner: bool
    worker_id: str | None
    lease_token: uuid.UUID | None
    reason: str


def is_safe_retry_risk(risk_class: str) -> bool:
    return risk_class in _SAFE_RETRY_RISKS


def is_unsafe_ambiguous_risk(risk_class: str) -> bool:
    return risk_class in _UNSAFE_RISKS


def can_safe_retry(*, attempt_count: int, max_attempts: int) -> bool:
    """True when another Attempt may be started after lease-expiry recovery."""
    if max_attempts < 1 or attempt_count < 0:
        return False
    return attempt_count < max_attempts


class ExecutionRecoveryService:
    """Atomic recovery/takeover for expired RUNNING AgentRequest Executions."""

    def __init__(self, session: AsyncSession, *, lease_seconds: int) -> None:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        self._session = session
        self._lease_seconds = lease_seconds
        self._executions = ExecutionRepository(session)

    async def list_expired_running_ids(
        self, *, limit: int, now: datetime | None = None
    ) -> list[uuid.UUID]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise AppError(
                code="VALIDATION_ERROR",
                message="recovery batch limit must be a positive integer.",
                status_code=400,
            )
        ts = now or datetime.now(UTC)
        stmt = (
            select(Execution.id)
            .where(
                Execution.status == ExecutionStatus.RUNNING.value,
                Execution.source_type == ExecutionSourceType.AGENT_REQUEST.value,
                Execution.lease_expires_at.is_not(None),
                Execution.lease_expires_at <= ts,
            )
            .order_by(Execution.lease_expires_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def recover(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        now: datetime | None = None,
    ) -> RecoveryOutcome:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)

        stmt = (
            select(Execution)
            .where(Execution.id == execution_id)
            .with_for_update(skip_locked=True)
        )
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            return RecoveryOutcome(
                execution_id=execution_id,
                decision=RecoveryDecision.NO_OP,
                taken_over=False,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason="MISSING_OR_LOCKED",
            )

        if execution.status != ExecutionStatus.RUNNING.value:
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=RecoveryDecision.NO_OP,
                taken_over=False,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason="NOT_RUNNING",
            )
        if (
            execution.lease_expires_at is None
            or _as_utc(execution.lease_expires_at) > _as_utc(ts)
        ):
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=RecoveryDecision.NO_OP,
                taken_over=False,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason="LEASE_NOT_EXPIRED",
            )

        try:
            step = await self._lock_foundation_step(execution)
            decision = await self._decide(execution=execution, step=step, now=ts)
        except AppError as exc:
            if exc.code != "RESOURCE_CONFLICT":
                raise
            self._assign_lease(execution, worker_id=worker, now=ts)
            await self._fail_inconsistent(
                execution=execution,
                step=await self._try_lock_single_step(execution),
                now=ts,
                message=exc.message,
            )
            await self._session.flush()
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=RecoveryDecision.FAIL_INCONSISTENT,
                taken_over=True,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason=exc.message,
            )

        if decision == RecoveryDecision.TAKEOVER_READY:
            token = self._assign_lease(execution, worker_id=worker, now=ts)
            await self._session.flush()
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=decision,
                taken_over=True,
                invoke_runner=True,
                worker_id=worker,
                lease_token=token,
                reason="STEP_READY",
            )

        if decision == RecoveryDecision.RESUME_ATTEMPT:
            token = self._assign_lease(execution, worker_id=worker, now=ts)
            attempt = await self._require_single_started_attempt(step)
            # Lineage already asserted in _decide before ownership transfer.
            attempt.worker_id = worker
            attempt.lease_expires_at = execution.lease_expires_at
            await self._session.flush()
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=decision,
                taken_over=True,
                invoke_runner=True,
                worker_id=worker,
                lease_token=token,
                reason="RESUME_STARTED_ATTEMPT",
            )

        if decision == RecoveryDecision.SAFE_RETRY:
            token = self._assign_lease(execution, worker_id=worker, now=ts)
            attempt = await self._require_single_started_attempt(step)
            tool_call = await self._require_started_tool_call(attempt)
            self._terminalize_orphan_attempt_tool_call(
                attempt=attempt,
                tool_call=tool_call,
                now=ts,
                terminal=StepAttemptStatus.FAILED.value,
                is_retryable=True,
                message=(
                    "Worker lease expired after tools/call evidence was started;"
                    " safe retry class allows a new Attempt."
                ),
            )
            step.status = StepStatus.READY.value
            step.error_code = None
            step.error_message = None
            step.finished_at = None
            step.lock_version += 1
            await self._session.flush()
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=decision,
                taken_over=True,
                invoke_runner=True,
                worker_id=worker,
                lease_token=token,
                reason="SAFE_RETRY_READY",
            )

        if decision == RecoveryDecision.FAIL_EXHAUSTED:
            self._assign_lease(execution, worker_id=worker, now=ts)
            attempt = await self._require_single_started_attempt(step)
            tool_call = await self._require_started_tool_call(attempt)
            self._terminalize_orphan_attempt_tool_call(
                attempt=attempt,
                tool_call=tool_call,
                now=ts,
                terminal=StepAttemptStatus.FAILED.value,
                is_retryable=False,
                message=(
                    "Worker lease expired after tools/call evidence was started;"
                    " max_attempts exhausted."
                ),
            )
            self._fail_execution_step(
                execution=execution,
                step=step,
                now=ts,
                error_code=_ERROR_CODE_LEASE_EXPIRED,
                error_message=(
                    "Worker lease expired with started ToolCall evidence;"
                    " max_attempts exhausted so remote call is not retried."
                ),
            )
            await self._session.flush()
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=decision,
                taken_over=True,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason="MAX_ATTEMPTS_EXHAUSTED",
            )

        if decision == RecoveryDecision.UNKNOWN_OUTCOME:
            self._assign_lease(execution, worker_id=worker, now=ts)
            attempt = await self._require_single_started_attempt(step)
            tool_call = await self._require_started_tool_call(attempt)
            self._terminalize_orphan_attempt_tool_call(
                attempt=attempt,
                tool_call=tool_call,
                now=ts,
                terminal=StepAttemptStatus.UNKNOWN_OUTCOME.value,
                is_retryable=False,
                message=(
                    "Worker lease expired after tools/call evidence was started;"
                    " unsafe risk class forbids automatic re-call."
                ),
            )
            step.status = StepStatus.UNKNOWN_OUTCOME.value
            step.error_code = _ERROR_CODE_LEASE_EXPIRED
            step.error_message = (
                "Worker lease expired with ambiguous external ToolCall evidence;"
                " outcome is unknown and is not automatically retried."
            )
            step.finished_at = ts
            step.lock_version += 1
            execution.status = ExecutionStatus.FAILED.value
            execution.error_code = _ERROR_CODE_LEASE_EXPIRED
            execution.error_message = (
                "MCP tool outcome is unknown after a possible external side effect;"
                " it is not automatically retried."
            )
            execution.finished_at = ts
            self._clear_lease(execution)
            execution.lock_version += 1
            await self._session.flush()
            return RecoveryOutcome(
                execution_id=execution.id,
                decision=decision,
                taken_over=True,
                invoke_runner=False,
                worker_id=None,
                lease_token=None,
                reason="UNSAFE_AMBIGUOUS_TOOL_CALL",
            )

        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"Unsupported recovery decision {decision}.",
            status_code=409,
        )

    async def _decide(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        now: datetime,
    ) -> RecoveryDecision:
        del now  # decision uses persisted evidence only
        if step.status == StepStatus.READY.value:
            return await self._decide_ready_checkpoint(execution=execution, step=step)

        if step.status != StepStatus.RUNNING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Step status {step.status} is not recoverable in this slice.",
                status_code=409,
            )

        attempts = await self._executions.list_attempts(step.id)
        started = [a for a in attempts if a.status == StepAttemptStatus.STARTED.value]
        if len(started) == 0:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="RUNNING Step has no STARTED Attempt.",
                status_code=409,
            )
        if len(started) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="RUNNING Step has multiple STARTED Attempts.",
                status_code=409,
            )
        attempt = started[0]
        if attempt.step_execution_id != step.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="StepAttempt lineage does not match Step.",
                status_code=409,
            )

        tool_calls = await self._executions.list_tool_calls(attempt.id)
        started_calls = [
            tc
            for tc in tool_calls
            if tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
        ]
        terminal_calls = [
            tc
            for tc in tool_calls
            if tc.normalized_status != ToolCallNormalizedStatus.STARTED.value
        ]
        if terminal_calls and step.status == StepStatus.RUNNING.value:
            # Terminal ToolCall with RUNNING Step is inconsistent for this slice.
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Terminal ToolCall exists while Step remains RUNNING.",
                status_code=409,
            )
        if len(started_calls) > 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="STARTED Attempt has multiple STARTED ToolCalls.",
                status_code=409,
            )
        if len(started_calls) == 0:
            if tool_calls:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Attempt has non-STARTED ToolCall evidence only.",
                    status_code=409,
                )
            assert_resume_attempt_lineage(
                execution=execution,
                step=step,
                attempt=attempt,
                worker_id=None,
            )
            return RecoveryDecision.RESUME_ATTEMPT

        tool_call = started_calls[0]
        if tool_call.step_attempt_id != attempt.id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ToolCall lineage does not match StepAttempt.",
                status_code=409,
            )
        if step.mcp_tool_version_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="RUNNING TOOL Step is missing mcp_tool_version_id.",
                status_code=409,
            )
        if tool_call.mcp_tool_version_id != step.mcp_tool_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ToolCall ToolVersion lineage does not match Step.",
                status_code=409,
            )

        risk_class, max_attempts = await self._load_risk_and_attempts(
            execution=execution, step=step
        )
        if is_unsafe_ambiguous_risk(risk_class):
            return RecoveryDecision.UNKNOWN_OUTCOME
        if is_safe_retry_risk(risk_class):
            if can_safe_retry(
                attempt_count=step.attempt_count, max_attempts=max_attempts
            ):
                return RecoveryDecision.SAFE_RETRY
            return RecoveryDecision.FAIL_EXHAUSTED
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=f"Unsupported risk_class for recovery: {risk_class}.",
            status_code=409,
        )

    async def _decide_ready_checkpoint(
        self, *, execution: Execution, step: ExecutionStep
    ) -> RecoveryDecision:
        """READY is a valid recovery checkpoint, including post-SAFE_RETRY crash.

        Historical terminal Attempts are allowed. STARTED Attempt/ToolCall is not.
        """
        attempts = await self._executions.list_attempts(step.id)
        started = [a for a in attempts if a.status == StepAttemptStatus.STARTED.value]
        if started:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="READY Step has STARTED Attempt.",
                status_code=409,
            )
        for attempt in attempts:
            if attempt.status not in _ATTEMPT_TERMINAL:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="READY Step has non-terminal Attempt.",
                    status_code=409,
                )
            if attempt.step_execution_id != step.id:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="StepAttempt lineage does not match Step.",
                    status_code=409,
                )
            tool_calls = await self._executions.list_tool_calls(attempt.id)
            if any(
                tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
                for tc in tool_calls
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="READY Step has STARTED ToolCall evidence.",
                    status_code=409,
                )

        if not attempts:
            if step.attempt_count != 0:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="READY Step attempt_count does not match Attempt history.",
                    status_code=409,
                )
            return RecoveryDecision.TAKEOVER_READY

        attempt_nos = sorted(a.attempt_no for a in attempts)
        expected = list(range(1, len(attempt_nos) + 1))
        if attempt_nos != expected:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="READY Step Attempt numbers are not contiguous.",
                status_code=409,
            )
        if step.attempt_count != attempt_nos[-1] or step.attempt_count != len(attempts):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="READY Step attempt_count does not match Attempt history.",
                status_code=409,
            )

        _risk_class, max_attempts = await self._load_risk_and_attempts(
            execution=execution, step=step
        )
        if can_safe_retry(
            attempt_count=step.attempt_count, max_attempts=max_attempts
        ):
            return RecoveryDecision.TAKEOVER_READY
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "READY Step attempt_count is exhausted;"
                " cannot takeover for another Attempt."
            ),
            status_code=409,
        )

    async def _load_risk_and_attempts(
        self, *, execution: Execution, step: ExecutionStep
    ) -> tuple[str, int]:
        """Pinned Execution.policy_snapshot is recovery retry authority.

        Mutable MCPToolPolicy is re-checked only at remote invocation preflight.
        """
        snapshot = execution.policy_snapshot
        if not isinstance(snapshot, dict):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution.policy_snapshot is missing or malformed.",
                status_code=409,
            )
        tool_policy = snapshot.get("tool_policy")
        if not isinstance(tool_policy, dict):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution.policy_snapshot.tool_policy is missing.",
                status_code=409,
            )
        risk_class = tool_policy.get("risk_class")
        max_attempts = tool_policy.get("max_attempts")
        try:
            RiskClass(risk_class)
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution.policy_snapshot risk_class is invalid.",
                status_code=409,
            ) from exc
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution.policy_snapshot max_attempts is invalid.",
                status_code=409,
            )

        snapshot_tool_id = tool_policy.get("mcp_tool_id")
        if not isinstance(snapshot_tool_id, str) or not snapshot_tool_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution.policy_snapshot mcp_tool_id is missing.",
                status_code=409,
            )
        if step.mcp_tool_version_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="TOOL Step is missing mcp_tool_version_id.",
                status_code=409,
            )
        tool_version = await MCPToolRepository(self._session).get_version(
            step.mcp_tool_version_id
        )
        if tool_version is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ToolVersion not found for recovery decision.",
                status_code=409,
            )
        if str(tool_version.mcp_tool_id) != snapshot_tool_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Execution.policy_snapshot mcp_tool_id does not match"
                    " Step ToolVersion lineage."
                ),
                status_code=409,
            )
        return str(risk_class), max_attempts
    async def _lock_foundation_step(self, execution: Execution) -> ExecutionStep:
        if execution.source_type != ExecutionSourceType.AGENT_REQUEST.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Recovery supports AgentRequest Executions only.",
                status_code=409,
            )
        steps = await self._executions.list_steps(execution.id)
        if len(steps) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentRequest Execution must contain exactly one Step.",
                status_code=409,
            )
        step = await self._executions.lock_step(steps[0].id)
        assert step is not None
        if (
            step.step_key != DETERMINISTIC_TOOL_STEP_ID
            or step.step_type != AuthorableStepType.TOOL.value
            or step.parent_step_id is not None
            or step.mcp_tool_version_id is None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ExecutionStep is inconsistent with AgentRequest TOOL foundation.",
                status_code=409,
            )
        return step

    async def _try_lock_single_step(self, execution: Execution) -> ExecutionStep | None:
        steps = await self._executions.list_steps(execution.id)
        if len(steps) != 1:
            return None
        return await self._executions.lock_step(steps[0].id)

    async def _require_single_started_attempt(self, step: ExecutionStep) -> StepAttempt:
        attempts = await self._executions.list_attempts(step.id)
        started = [a for a in attempts if a.status == StepAttemptStatus.STARTED.value]
        if len(started) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Expected exactly one STARTED Attempt.",
                status_code=409,
            )
        attempt = await self._executions.get_attempt_with_lock(started[0].id)
        assert attempt is not None
        return attempt

    async def _require_started_tool_call(self, attempt: StepAttempt) -> ToolCall:
        tool_calls = await self._executions.list_tool_calls(attempt.id)
        started = [
            tc
            for tc in tool_calls
            if tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
        ]
        if len(started) != 1:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Expected exactly one STARTED ToolCall.",
                status_code=409,
            )
        tool_call = await self._executions.get_tool_call_with_lock(started[0].id)
        assert tool_call is not None
        return tool_call

    def _assign_lease(
        self, execution: Execution, *, worker_id: str, now: datetime
    ) -> uuid.UUID:
        token = uuid.uuid4()
        expires_at = now + timedelta(seconds=self._lease_seconds)
        execution.worker_id = worker_id
        execution.lease_token = token
        execution.lease_expires_at = expires_at
        execution.heartbeat_at = now
        execution.lock_version += 1
        # Do not rewrite started_at / queued_at.
        return token

    def _clear_lease(self, execution: Execution) -> None:
        execution.worker_id = None
        execution.lease_token = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None

    def _terminalize_orphan_attempt_tool_call(
        self,
        *,
        attempt: StepAttempt,
        tool_call: ToolCall,
        now: datetime,
        terminal: str,
        is_retryable: bool,
        message: str,
    ) -> None:
        tool_call.normalized_status = terminal
        tool_call.finished_at = now
        attempt.status = terminal
        attempt.error_layer = _ERROR_LAYER
        attempt.error_code = _ERROR_CODE_LEASE_EXPIRED
        attempt.error_message = message
        attempt.is_retryable = is_retryable
        attempt.finished_at = now

    def _fail_execution_step(
        self,
        *,
        execution: Execution,
        step: ExecutionStep,
        now: datetime,
        error_code: str,
        error_message: str,
    ) -> None:
        step.status = StepStatus.FAILED.value
        step.error_code = error_code
        step.error_message = error_message
        step.finished_at = now
        step.lock_version += 1
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = error_code
        execution.error_message = error_message
        execution.finished_at = now
        self._clear_lease(execution)
        execution.lock_version += 1

    async def _fail_inconsistent(
        self,
        *,
        execution: Execution,
        step: ExecutionStep | None,
        now: datetime,
        message: str,
    ) -> None:
        # Scan the whole Execution so multi-step/corruption cases still surface
        # STARTED ToolCall send evidence.
        steps = await self._executions.list_steps(execution.id)
        started_tool_call_pairs: list[tuple[StepAttempt, ToolCall]] = []
        for step_row in steps:
            attempts = await self._executions.list_attempts(step_row.id)
            for attempt_row in attempts:
                tool_calls = await self._executions.list_tool_calls(attempt_row.id)
                for tc in tool_calls:
                    if tc.normalized_status == ToolCallNormalizedStatus.STARTED.value:
                        started_tool_call_pairs.append((attempt_row, tc))

        ambiguous_side_effect = bool(started_tool_call_pairs)
        attempt_terminal = (
            StepAttemptStatus.UNKNOWN_OUTCOME.value
            if ambiguous_side_effect
            else StepAttemptStatus.FAILED.value
        )
        step_terminal = (
            StepStatus.UNKNOWN_OUTCOME.value
            if ambiguous_side_effect
            else StepStatus.FAILED.value
        )

        # Possible external side-effect evidence wins: parent Attempt is forced
        # to UNKNOWN_OUTCOME even if it was already FAILED/TIMED_OUT/etc.
        for attempt_row, tool_call_row in started_tool_call_pairs:
            locked_attempt = await self._executions.get_attempt_with_lock(attempt_row.id)
            if locked_attempt is not None:
                locked_attempt.status = StepAttemptStatus.UNKNOWN_OUTCOME.value
                locked_attempt.error_layer = _ERROR_LAYER
                locked_attempt.error_code = _ERROR_CODE_INCONSISTENT
                locked_attempt.error_message = message
                locked_attempt.is_retryable = False
                if locked_attempt.finished_at is None:
                    locked_attempt.finished_at = now
            locked_tc = await self._executions.get_tool_call_with_lock(tool_call_row.id)
            if locked_tc is not None:
                locked_tc.normalized_status = (
                    ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
                )
                locked_tc.finished_at = now

        target_steps = steps if steps else ([step] if step is not None else [])
        for step_row in target_steps:
            if step_row is None or step_row.status in _STEP_TERMINAL:
                continue
            locked_step = await self._executions.lock_step(step_row.id)
            if locked_step is None or locked_step.status in _STEP_TERMINAL:
                continue
            locked_step.status = step_terminal
            locked_step.error_code = _ERROR_CODE_INCONSISTENT
            locked_step.error_message = message
            locked_step.finished_at = now
            locked_step.lock_version += 1

            if not ambiguous_side_effect:
                started = await self._executions.get_started_attempt(locked_step.id)
                if started is not None:
                    locked = await self._executions.get_attempt_with_lock(started.id)
                    if (
                        locked is not None
                        and locked.status == StepAttemptStatus.STARTED.value
                    ):
                        locked.status = attempt_terminal
                        locked.error_layer = _ERROR_LAYER
                        locked.error_code = _ERROR_CODE_INCONSISTENT
                        locked.error_message = message
                        locked.is_retryable = False
                        locked.finished_at = now

        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = _ERROR_CODE_INCONSISTENT
        execution.error_message = message
        execution.finished_at = now
        self._clear_lease(execution)
        execution.lock_version += 1
