"""McpToolRunner — TOOL Step Attempt MCP execution (docs/04 §14, docs/05 §13.6).

Phase A (TX1, short): lock Execution/Step, verify lease, start/reuse the
StepAttempt (READY→RUNNING via ``ToolStepAttemptService.start``), re-run
current-tool-executable + confirmation/approval fail-closed checks on replay,
verify CURRENT + STREAMABLE_HTTP only, create ``ToolCall`` STARTED, and commit
before any network I/O.

Phase B1 (no DB transaction): resolve the server auth secret and materialize
SECRET_REF tool arguments in memory only.

Phase B2 (short TX, final pre-send gate): re-lock Execution and re-run canonical
``assert_source_tool_executable`` plus lease/lineage/prepared-invocation drift
checks immediately before the remote call. Mutable authorization/policy changes
after Phase A are fail-closed here. This gate does not claim absolute
linearizability between DB commit and the subsequent socket write.

Phase B3 (network, no DB transaction held): call ``CurrentMCPClient.call_tool``
with a concurrent lease-heartbeat task.

Phase C (TX2, short, FOR UPDATE fencing): re-lock the same Execution/Step/
Attempt/ToolCall rows, verify worker/lease/lineage has not moved, and apply
the terminal transition atomically across ToolCall/StepAttempt/ExecutionStep/
Execution per the canonical risk_class / outcome_unknown decision matrix.

Never Celery-retries MCP failures. Never auto-retries ``UNKNOWN_OUTCOME``.
Execution never receives ``UNKNOWN_OUTCOME`` (not a canonical Execution
status) — ambiguous outcomes surface as Execution ``FAILED`` with an
explanatory ``error_message``.

Bounded safe retry (FNC-EXE-006): for READ_ONLY + retryable MCPClientError,
Phase C may checkpoint (terminal Attempt/ToolCall + Step READY + Execution
RUNNING) and loop into a new Attempt/ToolCall via the same prepare → final
pre-send → call path. max_attempts counts total Attempts. Step timeout is
total across Attempts from Step.started_at.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.approval.evidence import require_valid_approved_evidence
from app.approval.wait import ApprovalWaitService
from app.core.errors import AppError
from app.core.secrets import ResolvedSecret, SecretResolver
from app.domain.enums import (
    CURRENT_MCP_PROTOCOL_VERSION,
    AuthorableStepType,
    BindingKind,
    ExecutionStatus,
    MCPAuthType,
    McpInputRequestStatus,
    MCPProtocolEra,
    MCPTransportType,
    RiskClass,
    SecretKind,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService, _as_utc, _normalize_worker_id
from app.execution.completion import (
    DISPOSITION_FATAL_EXECUTION_FAILURE,
    DISPOSITION_KNOWN_STEP_FAILURE,
    DISPOSITION_RETRY,
    DISPOSITION_SUCCESS,
    DISPOSITION_WAIT,
)
from app.execution.lineage import assert_resume_attempt_lineage
from app.execution.mrtr_constants import MAX_MRTR_ROUNDS
from app.execution.mrtr_wait import assert_durable_waiting_input
from app.execution.result_validator import validate_tool_result
from app.execution.retry_decision import (
    decide_safe_transient_retry,
    pinned_backoff_policy,
    pinned_max_attempts,
    pinned_risk_class,
    remaining_step_timeout_ms,
    step_timeout_budget_exhausted,
)
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.execution.runtime_preflight import (
    assert_answered_plan_confirmation,
    assert_source_tool_executable,
)
from app.execution.secret_materialize import materialize_tool_arguments
from app.execution.secret_redaction import (
    collect_protected_plaintexts,
    contains_protected_plaintext,
    redact_text,
    sanitize_for_persistence,
)
from app.execution.tool_step_attempt import ApprovalWaitOutcome, ToolStepAttemptService
from app.mcp.auth_headers import build_mcp_auth_headers
from app.mcp.contracts import NormalizedInputRequired, NormalizedToolResult
from app.mcp.current import CurrentMCPClient
from app.mcp.errors import MCPClientError
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.plan_validation import PlanValidationRepository

logger = logging.getLogger(__name__)

_REASON_SAFE_RETRY_READY = "SAFE_RETRY_READY"
_REASON_WAITING_INPUT = "WAITING_INPUT"
_SAFE_RISK_CLASSES = frozenset({RiskClass.READ_ONLY.value, RiskClass.IDEMPOTENT_WRITE.value})
_STEP_TERMINAL_STATUSES = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.UNKNOWN_OUTCOME.value,
        StepStatus.CANCELLED.value,
        StepStatus.SKIPPED.value,
    }
)
_MIN_HEARTBEAT_INTERVAL_SECONDS = 5

SecretResolverFactory = Callable[[AsyncSession], SecretResolver]


class _PreSendFailClosed(MCPClientError):
    """A fail-closed decision made before any network call — never ambiguous."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(
            error_layer="PROTOCOL",
            error_code=error_code,
            message=message,
            retryable=False,
            outcome_unknown=False,
        )


class _NoSecretResolver:
    """Literal-only path — never touches the secret store or master key."""

    async def resolve(self, secret_id: uuid.UUID) -> None:
        raise AppError(
            code="SECRET_UNAVAILABLE",
            message="Secret material is unavailable; remote call is fail-closed.",
            status_code=409,
        )


@dataclass(frozen=True, slots=True)
class ToolRunOutcome:
    """Per-Step runner outcome. Execution aggregation is owned by the orchestrator.

    ``disposition`` is an internal classification (not a public API enum):
    SUCCESS / KNOWN_STEP_FAILURE / FATAL_EXECUTION_FAILURE / WAIT / RETRY / NOOP.
    """

    execution_id: uuid.UUID
    step_execution_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    tool_call_id: uuid.UUID | None
    mcp_called: bool
    terminal_status: str | None
    reason: str
    disposition: str = "NOOP"


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    execution_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID
    tool_call_id: uuid.UUID
    remote_request_id: str
    mcp_server_id: uuid.UUID
    mcp_tool_version_id: uuid.UUID
    endpoint: str
    tool_name: str
    timeout_ms: int
    policy_timeout_ms: int
    resolved_input: dict[str, Any]
    auth_type: str
    auth_secret_id: uuid.UUID | None
    protocol_era: str
    transport_type: str
    risk_class: str
    output_schema: Any
    max_result_bytes: int
    worker_id: str
    lease_token: uuid.UUID
    # MRTR resume (PR #41): durable ANSWERED response + exact opaque requestState.
    # None means initial tools/call (no inputResponses / requestState).
    mrtr_input_request_id: uuid.UUID | None = None
    mrtr_input_responses: dict[str, Any] | None = None
    mrtr_request_state: Any = None
    # DAG wave mode: keep Execution RUNNING + lease so siblings can settle.
    defer_execution_terminalization: bool = False
    # Multi-Step DAG: never enter Execution-level WAITING_* states.
    forbid_execution_wait: bool = False


def _apply_or_defer_execution_fatal(
    execution: Execution,
    *,
    error_code: str | None,
    error_message: str | None,
    now: datetime,
    defer: bool,
) -> None:
    """Terminalize Execution or only heartbeat when orchestrator owns the wave."""
    if defer:
        execution.heartbeat_at = now
        execution.lock_version += 1
        return
    execution.status = ExecutionStatus.FAILED.value
    execution.error_code = error_code
    execution.error_message = error_message
    execution.result_summary = None
    execution.finished_at = now
    execution.worker_id = None
    execution.lease_token = None
    execution.lease_expires_at = None
    execution.heartbeat_at = None
    execution.lock_version += 1


def _prepared_invocation_lineage_matches(
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    tool_call: ToolCall,
    prepared: _PreparedCall,
) -> bool:
    return (
        step.execution_id == execution.id
        and attempt.step_execution_id == step.id
        and tool_call.step_attempt_id == attempt.id
        and tool_call.remote_request_id == prepared.remote_request_id
        and tool_call.mcp_server_id == prepared.mcp_server_id
        and tool_call.mcp_tool_version_id == prepared.mcp_tool_version_id
        and tool_call.normalized_status == ToolCallNormalizedStatus.STARTED.value
        and step.status == StepStatus.RUNNING.value
        and attempt.status == StepAttemptStatus.STARTED.value
    )


@dataclass(frozen=True, slots=True)
class _PrepareResult:
    prepared: _PreparedCall | None = None
    outcome: ToolRunOutcome | None = None


def _classify_mcp_failure(exc: MCPClientError, *, risk_class: str) -> str:
    """Map an MCP failure to a canonical Step/Attempt/ToolCall terminal status."""
    safe_risk = risk_class in _SAFE_RISK_CLASSES
    if exc.outcome_unknown and not safe_risk:
        return StepStatus.UNKNOWN_OUTCOME.value
    if exc.error_layer == "TIMEOUT" and safe_risk:
        return StepStatus.TIMED_OUT.value
    return StepStatus.FAILED.value


def _serialize_result_inline(result: NormalizedToolResult) -> dict[str, Any]:
    return {
        "content": result.content,
        "structured_content": result.structured_content,
        "metadata": result.metadata,
        "result_type": result.result_type,
        "duration_ms": result.duration_ms,
    }


def _plan_timeout_seconds(step: ExecutionStep) -> int | None:
    timeout = step.step_snapshot.get("timeout_seconds")
    if timeout is None:
        return None
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Step timeout_seconds is invalid.",
            status_code=409,
        )
    return timeout


def _resolved_input_needs_secret(resolved_input: dict[str, Any]) -> bool:
    return any(
        isinstance(value, dict) and value.get("kind") == BindingKind.SECRET_REF.value
        for value in resolved_input.values()
    )


def _payload_needs_secret(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(value, dict) and value.get("kind") == BindingKind.SECRET_REF.value
        for value in payload.values()
    )


async def _load_answered_mrtr_resume(
    *,
    inputs: MCPInputRequestRepository,
    executions: ExecutionRepository,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
) -> tuple[uuid.UUID, dict[str, Any], Any] | None:
    """Return (input_request_id, response_payload, request_state) for MRTR resume.

    Requires exactly one ANSWERED request whose round_no matches the count of
    SUCCEEDED ToolCalls on the STARTED Attempt, with no OPEN/STARTED ToolCall.
    """
    open_rows = await inputs.list_open_for_step(
        execution_id=execution.id, step_execution_id=step.id
    )
    if open_rows:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Cannot resume MRTR while an OPEN MCPInputRequest exists.",
            status_code=409,
        )
    history = await inputs.list_for_step(
        execution_id=execution.id, step_execution_id=step.id
    )
    answered = [
        row
        for row in history
        if row.status == McpInputRequestStatus.ANSWERED.value
        and row.step_attempt_id == attempt.id
        and isinstance(row.response_payload, dict)
        and row.response_payload
    ]
    if not answered:
        return None
    # Resume the highest answered round that still matches ToolCall evidence.
    answered.sort(key=lambda r: (r.round_no, r.answered_at or r.requested_at))
    request = answered[-1]
    tool_calls = await executions.list_tool_calls(attempt.id)
    if any(
        tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
        for tc in tool_calls
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="MRTR resume must not retain a STARTED ToolCall.",
            status_code=409,
        )
    succeeded = [
        tc
        for tc in tool_calls
        if tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
    ]
    if len(tool_calls) != len(succeeded) or len(succeeded) != request.round_no:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="MRTR resume ToolCall evidence does not match ANSWERED round_no.",
            status_code=409,
        )
    return request.id, dict(request.response_payload), request.request_state


def _auth_material_plaintexts(resolved: ResolvedSecret | None) -> list[str]:
    """Plaintext credential fragments from a resolved auth secret (memory only)."""
    if resolved is None:
        return []
    values: list[str] = []
    material = resolved.material
    if resolved.kind == SecretKind.API_KEY.value:
        value = material.get("value")
        if isinstance(value, str) and value:
            values.append(value)
    elif resolved.kind == SecretKind.OAUTH_TOKEN_SET.value:
        token = material.get("access_token")
        if isinstance(token, str) and token:
            values.append(token)
    elif resolved.kind == SecretKind.BASIC_AUTH.value:
        password = material.get("password")
        if isinstance(password, str) and password:
            values.append(password)
    return values


async def _assert_confirmation_evidence(
    session: AsyncSession,
    execution: Execution,
    policy_snapshot: dict[str, Any],
) -> None:
    if execution.agent_request_id is None or execution.plan_validation_run_id is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="PLAN_CONFIRMATION evidence lineage missing on Execution.",
            status_code=409,
        )
    validation = await PlanValidationRepository(session).get_by_id(
        execution.plan_validation_run_id
    )
    if validation is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Pinned PlanValidationRun not found for confirmation check.",
            status_code=409,
        )
    await assert_answered_plan_confirmation(
        session,
        agent_request_id=execution.agent_request_id,
        requester_id=execution.requester_id,
        plan_generation_run_id=validation.plan_generation_run_id,
        plan_hash=execution.plan_hash,
        policy_snapshot=policy_snapshot,
    )


class McpToolRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        mcp_client: CurrentMCPClient,
        secret_resolver_factory: SecretResolverFactory,
        lease_seconds: int,
        result_inline_max_bytes: int,
    ) -> None:
        self._session_factory = session_factory
        self._mcp_client = mcp_client
        self._secret_resolver_factory = secret_resolver_factory
        self._lease_seconds = lease_seconds
        self._result_inline_max_bytes = result_inline_max_bytes

    async def run_claimed_execution(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
    ) -> ToolRunOutcome:
        """Run TOOL/JOIN DAG wave orchestration under a claimed Execution lease.

        Graph progression, JOIN reconciliation, and Execution completion are
        owned by ``ExecutionOrchestrator``. This method remains the Celery/task
        entry (one task per Execution, not per Step).
        """
        from app.execution.orchestrator import ExecutionOrchestrator

        return await ExecutionOrchestrator(
            session_factory=self._session_factory,
            tool_runner=self,
        ).run(
            execution_id=execution_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )

    async def run_claimed_tool_step(
        self,
        *,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
        defer_execution_terminalization: bool = False,
        forbid_execution_wait: bool = False,
    ) -> ToolRunOutcome:
        """Invoke MCP for exactly one TOOL Step (safe-retry loop included).

        On TOOL SUCCEEDED or ordinary known failure/timeout this does **not**
        terminalize the Execution — ``ExecutionOrchestrator`` applies
        ErrorPolicy / ALL_REQUIRED. Mandatory-fatal dispositions
        (UNKNOWN_OUTCOME, pre-send security/integrity fail-closed) still
        terminalize Execution here unless ``defer_execution_terminalization``
        is set (DAG wave mode — siblings must settle first).

        ``forbid_execution_wait`` fail-closes multi-Step Approval/MRTR waits
        with ``DAG_WAIT_UNSUPPORTED`` / conservative UNKNOWN_OUTCOME.
        """
        while True:
            prep = await self._prepare(
                execution_id=execution_id,
                step_execution_id=step_execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
                defer_execution_terminalization=defer_execution_terminalization,
                forbid_execution_wait=forbid_execution_wait,
            )
            if prep.prepared is None:
                assert prep.outcome is not None
                return prep.outcome

            prepared = prep.prepared
            protected: tuple[str, ...] = ()
            try:
                (
                    auth_headers,
                    arguments,
                    input_responses,
                    protected,
                ) = await self._resolve_secrets(prepared)
            except AppError as exc:
                call_error = _PreSendFailClosed(error_code=exc.code, message=exc.message)
                return await self._finalize_locked(
                    prepared,
                    call_error=call_error,
                    result=None,
                    response_meta=None,
                    first_byte_at=None,
                    protected=(),
                )

            try:
                # Deterministic TOCTOU test seam (production no-op): mutate mutable
                # authorization/policy after Phase A and secret materialize, before the
                # final pre-send gate.
                await self._after_phase_a_before_final_gate(prepared)

                gate = await self._final_pre_send_gate(prepared)
                if isinstance(gate, ToolRunOutcome):
                    # Lease lost (NO_OP) or fail-closed terminalization already committed
                    # in the final gate transaction — never overwrite a new owner's state.
                    return gate
                if isinstance(gate, _PreSendFailClosed):
                    return await self._finalize_locked(
                        prepared,
                        call_error=gate,
                        result=None,
                        response_meta=None,
                        first_byte_at=None,
                        protected=protected,
                    )

                result, response_meta, first_byte_at, call_error = await self._call_remote(
                    prepared,
                    auth_headers,
                    arguments,
                    input_responses=input_responses,
                )
                if isinstance(result, NormalizedInputRequired):
                    return await self._finalize_input_required(
                        prepared,
                        mrtr=result,
                        response_meta=response_meta,
                        first_byte_at=first_byte_at,
                        protected=protected,
                    )
                outcome = await self._finalize_locked(
                    prepared,
                    call_error=call_error,
                    result=result,
                    response_meta=response_meta,
                    first_byte_at=first_byte_at,
                    protected=protected,
                )
                if outcome.reason == _REASON_SAFE_RETRY_READY:
                    continue
                return outcome
            finally:
                auth_headers.clear()
                arguments.clear()
                protected = ()

    async def _after_phase_a_before_final_gate(self, prepared: _PreparedCall) -> None:
        """Production no-op. Integration tests monkeypatch this to mutate DB state."""
        del prepared

    def _final_gate_fail_closed_lineage_corruption(
        self,
        *,
        execution: Execution,
        step: ExecutionStep | None,
        attempt: StepAttempt | None,
        tool_call: ToolCall | None,
        prepared: _PreparedCall,
        now: datetime,
    ) -> ToolRunOutcome:
        """Valid lease owner + inconsistent/missing evidence — fail closed, MCP 0.

        Already-terminal Step/Attempt/ToolCall rows are preserved. Non-terminal
        evidence is FAILED. Execution is always FAILED with lease cleared so the
        runner cannot strand RUNNING ownership.
        """
        error_code = "RESOURCE_CONFLICT"
        error_message = "Final pre-send invocation lineage is inconsistent."

        if step is not None and step.status not in _STEP_TERMINAL_STATUSES:
            step.status = StepStatus.FAILED.value
            step.error_code = error_code
            step.error_message = error_message
            step.finished_at = now
            step.lock_version += 1

        if attempt is not None and attempt.status == StepAttemptStatus.STARTED.value:
            attempt.status = StepAttemptStatus.FAILED.value
            attempt.error_layer = "PROTOCOL"
            attempt.error_code = error_code
            attempt.error_message = error_message
            attempt.is_retryable = False
            attempt.finished_at = now

        if (
            tool_call is not None
            and tool_call.normalized_status == ToolCallNormalizedStatus.STARTED.value
        ):
            tool_call.normalized_status = ToolCallNormalizedStatus.FAILED.value
            tool_call.finished_at = now

        _apply_or_defer_execution_fatal(
            execution,
            error_code=error_code,
            error_message=error_message,
            now=now,
            defer=prepared.defer_execution_terminalization,
        )

        return ToolRunOutcome(
            execution_id=execution.id,
            step_execution_id=step.id if step is not None else prepared.step_id,
            attempt_id=attempt.id if attempt is not None else prepared.attempt_id,
            tool_call_id=(
                tool_call.id if tool_call is not None else prepared.tool_call_id
            ),
            mcp_called=False,
            terminal_status=StepStatus.FAILED.value,
            reason=error_code,
            disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
        )

    async def _final_pre_send_gate(
        self, prepared: _PreparedCall
    ) -> ToolRunOutcome | _PreSendFailClosed | None:
        """Short TX: revalidate lease/lineage + current authz/policy before network.

        Returns:
          - ``None`` — gate passed; caller may invoke ``call_tool`` immediately
          - ``_PreSendFailClosed`` — still owns lease; caller must terminalize
            via ``_finalize_locked`` without a remote call
          - ``ToolRunOutcome`` — lease/ownership lost (NO_OP), or lineage corruption
            fail-closed terminalization committed in-gate (do not finalize again)
        """
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(prepared.execution_id)
                now = datetime.now(UTC)
                if (
                    execution is None
                    or execution.status != ExecutionStatus.RUNNING.value
                    or execution.worker_id != prepared.worker_id
                    or execution.lease_token != prepared.lease_token
                    or execution.lease_expires_at is None
                    or _as_utc(execution.lease_expires_at) <= _as_utc(now)
                ):
                    return self._noop(
                        prepared.execution_id,
                        "LEASE_MISMATCH",
                        step_id=prepared.step_id,
                        status=None if execution is None else execution.status,
                    )

                step = await executions.lock_step(prepared.step_id)
                attempt = await executions.get_attempt_with_lock(prepared.attempt_id)
                tool_call = await executions.get_tool_call_with_lock(
                    prepared.tool_call_id
                )
                if step is None or attempt is None or tool_call is None:
                    return self._final_gate_fail_closed_lineage_corruption(
                        execution=execution,
                        step=step,
                        attempt=attempt,
                        tool_call=tool_call,
                        prepared=prepared,
                        now=now,
                    )
                if not _prepared_invocation_lineage_matches(
                    execution=execution,
                    step=step,
                    attempt=attempt,
                    tool_call=tool_call,
                    prepared=prepared,
                ):
                    # Ownership valid but evidence is terminal, missing identity
                    # fields, or otherwise inconsistent — never treat this as a
                    # successful Phase C completion (that would have cleared the
                    # lease). Fail closed and clear RUNNING strand.
                    return self._final_gate_fail_closed_lineage_corruption(
                        execution=execution,
                        step=step,
                        attempt=attempt,
                        tool_call=tool_call,
                        prepared=prepared,
                        now=now,
                    )

                try:
                    plan_step_id = str(step.step_snapshot.get("id") or step.step_key)
                    expected_policy = get_expected_tool_policy_snapshot(
                        execution,
                        plan_step_id=plan_step_id,
                        tool_version_id=step.mcp_tool_version_id,
                    )
                    authz = await assert_source_tool_executable(
                        session,
                        execution=execution,
                        tool_version_id=step.mcp_tool_version_id,
                        expected_policy_snapshot=expected_policy,
                        plan_timeout_seconds=_plan_timeout_seconds(step),
                    )
                    if authz.tool_policy.requires_approval:
                        if authz.approval_policy is None:
                            return _PreSendFailClosed(
                                error_code="EXECUTION_PRECONDITION_FAILED",
                                message="requires_approval인데 ApprovalPolicy 없음.",
                            )
                        try:
                            await require_valid_approved_evidence(
                                session,
                                execution=execution,
                                step=step,
                                tool_policy=authz.tool_policy,
                                approval_policy=authz.approval_policy,
                            )
                        except AppError as exc:
                            return _PreSendFailClosed(
                                error_code=exc.code, message=exc.message
                            )
                    if authz.confirmation_required:
                        await _assert_confirmation_evidence(
                            session, execution, authz.policy_snapshot
                        )
                except AppError as exc:
                    return _PreSendFailClosed(
                        error_code=exc.code, message=exc.message
                    )

                # Prepared invocation-critical drift (endpoint/auth/transport not
                # covered by policy_snapshot equality alone). Fail closed — do not
                # auto-reroute to a new endpoint with stale credentials.
                server = authz.server
                logical_tool = authz.logical_tool
                tool_policy = authz.tool_policy
                if (
                    server.id != prepared.mcp_server_id
                    or authz.tool_version.id != prepared.mcp_tool_version_id
                    or logical_tool.remote_name != prepared.tool_name
                    or str(server.endpoint_url or "") != prepared.endpoint
                    or server.protocol_era != prepared.protocol_era
                    or server.transport_type != prepared.transport_type
                    or server.auth_type != prepared.auth_type
                    or server.auth_secret_id != prepared.auth_secret_id
                    or tool_policy.risk_class != prepared.risk_class
                    or tool_policy.timeout_ms != prepared.policy_timeout_ms
                    or tool_policy.max_result_bytes != prepared.max_result_bytes
                ):
                    return _PreSendFailClosed(
                        error_code="EXECUTION_PRECONDITION_FAILED",
                        message=(
                            "Prepared MCP invocation drifted from current"
                            " Tool/Server/policy state."
                        ),
                    )

                if (
                    server.protocol_era != MCPProtocolEra.CURRENT.value
                    or server.transport_type != MCPTransportType.STREAMABLE_HTTP.value
                ):
                    return _PreSendFailClosed(
                        error_code="MCP_TRANSPORT_UNSUPPORTED",
                        message=(
                            "MCP Tool Runner supports CURRENT + STREAMABLE_HTTP"
                            " servers only."
                        ),
                    )
                if not server.endpoint_url:
                    return _PreSendFailClosed(
                        error_code="MCP_ENDPOINT_MISSING",
                        message=(
                            "MCP Server endpoint_url is required for STREAMABLE_HTTP"
                            " tools/call."
                        ),
                    )

                return None

    # -- Phase A -----------------------------------------------------------

    async def _prepare(
        self,
        *,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
        defer_execution_terminalization: bool = False,
        forbid_execution_wait: bool = False,
    ) -> _PrepareResult:
        worker = _normalize_worker_id(worker_id)
        defer = defer_execution_terminalization
        forbid_wait = forbid_execution_wait
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(execution_id)
                if execution is None:
                    return _PrepareResult(outcome=self._noop(execution_id, "MISSING"))

                now = datetime.now(UTC)
                step = await executions.lock_step(step_execution_id)
                if step is None or step.execution_id != execution.id:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Step does not belong to the claimed Execution.",
                        status_code=409,
                    )
                if step.step_type != AuthorableStepType.TOOL.value:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="McpToolRunner supports TOOL Steps only.",
                        status_code=409,
                    )

                # One-sided WAITING_APPROVAL is atomicity corruption — never
                # normalize it into a successful wait outcome.
                exec_waiting = (
                    execution.status == ExecutionStatus.WAITING_APPROVAL.value
                )
                step_waiting = step.status == StepStatus.WAITING_APPROVAL.value
                if exec_waiting != step_waiting:
                    return _PrepareResult(
                        outcome=ToolRunOutcome(
                            execution_id=execution.id,
                            step_execution_id=step.id,
                            attempt_id=None,
                            tool_call_id=None,
                            mcp_called=False,
                            terminal_status=None,
                            reason="RESOURCE_CONFLICT",
                        )
                    )
                if exec_waiting and step_waiting:
                    # Require exactly one PENDING ApprovalRequest — do not treat
                    # missing evidence as a valid wait.
                    try:
                        await ApprovalWaitService(session).reuse_pending(
                            execution=execution, step=step
                        )
                    except AppError as exc:
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=None,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=None,
                                reason=exc.code,
                            )
                        )
                    return _PrepareResult(
                        outcome=self._noop(
                            execution.id,
                            "WAITING_APPROVAL",
                            step_id=step.id,
                            status=step.status,
                        )
                    )

                # Durable MRTR wait — lease absent; never re-call MCP.
                exec_mrtr = execution.status == ExecutionStatus.WAITING_INPUT.value
                step_mrtr = step.status == StepStatus.WAITING_INPUT.value
                if exec_mrtr or step_mrtr:
                    if not (exec_mrtr and step_mrtr):
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=None,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=None,
                                reason="RESOURCE_CONFLICT",
                            )
                        )
                    try:
                        await assert_durable_waiting_input(
                            executions=executions,
                            inputs=MCPInputRequestRepository(session),
                            execution=execution,
                            step=step,
                        )
                    except AppError as exc:
                        if exc.code == "RESOURCE_CONFLICT":
                            return _PrepareResult(
                                outcome=ToolRunOutcome(
                                    execution_id=execution.id,
                                    step_execution_id=step.id,
                                    attempt_id=None,
                                    tool_call_id=None,
                                    mcp_called=False,
                                    terminal_status=None,
                                    reason="RESOURCE_CONFLICT",
                                )
                            )
                        raise
                    return _PrepareResult(
                        outcome=self._noop(
                            execution.id,
                            _REASON_WAITING_INPUT,
                            step_id=step.id,
                            status=step.status,
                        )
                    )

                if (
                    execution.status != ExecutionStatus.RUNNING.value
                    or execution.worker_id != worker
                    or execution.lease_token != lease_token
                    or execution.lease_expires_at is None
                    or _as_utc(execution.lease_expires_at) <= _as_utc(now)
                ):
                    return _PrepareResult(
                        outcome=self._noop(
                            execution.id, "LEASE_MISMATCH", status=execution.status
                        )
                    )

                if step.status in _STEP_TERMINAL_STATUSES:
                    return _PrepareResult(
                        outcome=self._noop(
                            execution.id,
                            "STEP_ALREADY_TERMINAL",
                            step_id=step.id,
                            status=step.status,
                        )
                    )
                if step.status not in {StepStatus.READY.value, StepStatus.RUNNING.value}:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=f"Step status {step.status} unsupported by MCP Tool Runner.",
                        status_code=409,
                    )

                # Total Step timeout budget spans Attempts; do not start another
                # Attempt/ToolCall when the budget is already exhausted.
                if step.status == StepStatus.READY.value and step_timeout_budget_exhausted(
                    step_started_at=step.started_at,
                    timeout_seconds=_plan_timeout_seconds(step),
                    now=now,
                ):
                    # Known TIMED_OUT — leave Execution RUNNING for ErrorPolicy.
                    step.status = StepStatus.TIMED_OUT.value
                    step.error_code = "STEP_TIMEOUT_EXCEEDED"
                    step.error_message = (
                        "Step total timeout budget exhausted before another Attempt."
                    )
                    step.finished_at = now
                    step.lock_version += 1
                    execution.heartbeat_at = now
                    execution.lock_version += 1
                    return _PrepareResult(
                        outcome=ToolRunOutcome(
                            execution_id=execution.id,
                            step_execution_id=step.id,
                            attempt_id=None,
                            tool_call_id=None,
                            mcp_called=False,
                            terminal_status=StepStatus.TIMED_OUT.value,
                            reason="STEP_TIMEOUT_EXCEEDED",
                            disposition=DISPOSITION_KNOWN_STEP_FAILURE,
                        )
                    )

                try:
                    attempt_outcome = await ToolStepAttemptService(session).start(
                        execution_id=execution.id,
                        step_execution_id=step.id,
                        worker_id=worker,
                        lease_token=lease_token,
                        now=now,
                        forbid_new_approval_wait=forbid_wait,
                    )
                except AppError as exc:
                    # Do not strand RUNNING + READY after recovery/claim when
                    # runtime preflight rejects (e.g. pinned policy_snapshot drift).
                    # Valid both-side wait already cleared the lease — do not FAILED it.
                    # One-sided WAITING_APPROVAL is corruption: leave state untouched.
                    exec_waiting = (
                        execution.status == ExecutionStatus.WAITING_APPROVAL.value
                    )
                    step_waiting = step.status == StepStatus.WAITING_APPROVAL.value
                    if exec_waiting and step_waiting:
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=None,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=StepStatus.WAITING_APPROVAL.value,
                                reason="WAITING_APPROVAL",
                            )
                        )
                    if exec_waiting or step_waiting:
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=None,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=None,
                                reason=exc.code,
                            )
                        )
                    # Orphan PENDING while still RUNNING/READY is durable
                    # inconsistency — do not terminalize or clear lease.
                    if (
                        exc.code == "RESOURCE_CONFLICT"
                        and execution.status == ExecutionStatus.RUNNING.value
                        and step.status == StepStatus.READY.value
                        and "PENDING ApprovalRequest already exists" in exc.message
                    ):
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=None,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=None,
                                reason=exc.code,
                            )
                        )
                    if step.status not in _STEP_TERMINAL_STATUSES:
                        step.status = StepStatus.FAILED.value
                        step.error_code = exc.code
                        step.error_message = exc.message
                        step.finished_at = now
                        step.lock_version += 1
                    if execution.status == ExecutionStatus.RUNNING.value:
                        _apply_or_defer_execution_fatal(
                            execution,
                            error_code=exc.code,
                            error_message=exc.message,
                            now=now,
                            defer=defer,
                        )
                    return _PrepareResult(
                        outcome=ToolRunOutcome(
                            execution_id=execution.id,
                            step_execution_id=step.id,
                            attempt_id=None,
                            tool_call_id=None,
                            mcp_called=False,
                            terminal_status=StepStatus.FAILED.value,
                            reason=exc.code,
                            disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
                        )
                    )

                if isinstance(attempt_outcome, ApprovalWaitOutcome):
                    if forbid_wait:
                        # Should be unreachable when forbid_new_approval_wait is set;
                        # fail closed without leaving a resumable DAG wait.
                        if step.status not in _STEP_TERMINAL_STATUSES:
                            step.status = StepStatus.FAILED.value
                            step.error_code = "DAG_WAIT_UNSUPPORTED"
                            step.error_message = (
                                "Multi-Step TOOL/JOIN DAG cannot enter "
                                "WAITING_APPROVAL."
                            )
                            step.finished_at = now
                            step.lock_version += 1
                        if execution.status == ExecutionStatus.RUNNING.value:
                            _apply_or_defer_execution_fatal(
                                execution,
                                error_code="DAG_WAIT_UNSUPPORTED",
                                error_message=step.error_message,
                                now=now,
                                defer=defer,
                            )
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=None,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=StepStatus.FAILED.value,
                                reason="DAG_WAIT_UNSUPPORTED",
                                disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
                            )
                        )
                    return _PrepareResult(
                        outcome=ToolRunOutcome(
                            execution_id=execution.id,
                            step_execution_id=step.id,
                            attempt_id=None,
                            tool_call_id=None,
                            mcp_called=False,
                            terminal_status=StepStatus.WAITING_APPROVAL.value,
                            reason="WAITING_APPROVAL",
                            disposition=DISPOSITION_WAIT,
                        )
                    )

                attempt = await executions.get_attempt(attempt_outcome.attempt_id)
                if attempt is None or attempt.status != StepAttemptStatus.STARTED.value:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="TOOL Step Attempt is not STARTED.",
                        status_code=409,
                    )

                existing_tool_calls = await executions.list_tool_calls(attempt.id)
                started_tool_calls = [
                    tc
                    for tc in existing_tool_calls
                    if tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
                ]
                if started_tool_calls:
                    # ToolCall STARTED is durable invocation evidence only.
                    # FNC-EXE-011 recovery must resolve this before the runner
                    # is invoked again; never reissue tools/call here.
                    return _PrepareResult(
                        outcome=ToolRunOutcome(
                            execution_id=execution.id,
                            step_execution_id=step.id,
                            attempt_id=attempt.id,
                            tool_call_id=started_tool_calls[0].id,
                            mcp_called=False,
                            terminal_status=None,
                            reason="TOOL_CALL_ALREADY_STARTED",
                        )
                    )

                mrtr_resume: tuple[uuid.UUID, dict[str, Any], Any] | None = None
                if existing_tool_calls:
                    # Terminal ToolCall(s) while RUNNING are only legal for MRTR
                    # resume (ANSWERED request + matching SUCCEEDED rounds).
                    try:
                        mrtr_resume = await _load_answered_mrtr_resume(
                            inputs=MCPInputRequestRepository(session),
                            executions=executions,
                            execution=execution,
                            step=step,
                            attempt=attempt,
                        )
                    except AppError as exc:
                        if step.status not in _STEP_TERMINAL_STATUSES:
                            step.status = StepStatus.FAILED.value
                            step.error_code = exc.code
                            step.error_message = exc.message
                            step.finished_at = now
                            step.lock_version += 1
                        if execution.status == ExecutionStatus.RUNNING.value:
                            _apply_or_defer_execution_fatal(
                                execution,
                                error_code=exc.code,
                                error_message=exc.message,
                                now=now,
                                defer=defer,
                            )
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=attempt.id,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=StepStatus.FAILED.value,
                                reason=exc.code,
                                disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
                            )
                        )
                    if mrtr_resume is None:
                        raise AppError(
                            code="RESOURCE_CONFLICT",
                            message=(
                                "ToolCall already terminal while Step remains RUNNING."
                            ),
                            status_code=409,
                        )

                if attempt_outcome.replayed:
                    # Replay skipped fresh READY start lineage checks; re-assert
                    # immutable plan/input snapshots before creating a ToolCall.
                    try:
                        assert_resume_attempt_lineage(
                            execution=execution,
                            step=step,
                            attempt=attempt,
                            steps=await executions.list_steps(execution.id),
                            worker_id=worker,
                        )
                    except AppError as exc:
                        if step.status not in _STEP_TERMINAL_STATUSES:
                            step.status = StepStatus.FAILED.value
                            step.error_code = exc.code
                            step.error_message = exc.message
                            step.finished_at = now
                            step.lock_version += 1
                        if execution.status == ExecutionStatus.RUNNING.value:
                            _apply_or_defer_execution_fatal(
                                execution,
                                error_code=exc.code,
                                error_message=exc.message,
                                now=now,
                                defer=defer,
                            )
                        return _PrepareResult(
                            outcome=ToolRunOutcome(
                                execution_id=execution.id,
                                step_execution_id=step.id,
                                attempt_id=attempt.id,
                                tool_call_id=None,
                                mcp_called=False,
                                terminal_status=StepStatus.FAILED.value,
                                reason=exc.code,
                                disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
                            )
                        )

                tool_version = await MCPToolRepository(session).get_version(
                    step.mcp_tool_version_id
                )
                if tool_version is None:
                    raise AppError(
                        code="EXECUTION_PRECONDITION_FAILED",
                        message="ToolVersion not found.",
                        status_code=409,
                    )
                logical_tool = await MCPToolRepository(session).get(tool_version.mcp_tool_id)
                if logical_tool is None:
                    raise AppError(
                        code="EXECUTION_PRECONDITION_FAILED",
                        message="MCP Tool not found.",
                        status_code=409,
                    )
                server = await MCPServerRepository(session).get(logical_tool.mcp_server_id)
                if server is None:
                    raise AppError(
                        code="EXECUTION_PRECONDITION_FAILED",
                        message="MCP Server not found.",
                        status_code=409,
                    )
                tool_policy = await MCPToolPolicyRepository(session).get_by_tool_id(
                    logical_tool.id
                )
                if tool_policy is None:
                    raise AppError(
                        code="EXECUTION_PRECONDITION_FAILED",
                        message="MCPToolPolicy not found.",
                        status_code=409,
                    )

                pre_send_failure: MCPClientError | None = None

                if attempt_outcome.replayed:
                    # Fresh READY->RUNNING starts already ran these checks inside
                    # ToolStepAttemptService.start(); replays did not, so re-run
                    # them here before ever issuing (or re-issuing) the network call.
                    try:
                        plan_step_id = str(step.step_snapshot.get("id") or step.step_key)
                        expected_policy = get_expected_tool_policy_snapshot(
                            execution,
                            plan_step_id=plan_step_id,
                            tool_version_id=step.mcp_tool_version_id,
                        )
                        authz = await assert_source_tool_executable(
                            session,
                            execution=execution,
                            tool_version_id=step.mcp_tool_version_id,
                            expected_policy_snapshot=expected_policy,
                            plan_timeout_seconds=_plan_timeout_seconds(step),
                        )
                        if authz.tool_policy.requires_approval:
                            if authz.approval_policy is None:
                                pre_send_failure = _PreSendFailClosed(
                                    error_code="EXECUTION_PRECONDITION_FAILED",
                                    message="requires_approval인데 ApprovalPolicy 없음.",
                                )
                            else:
                                try:
                                    await require_valid_approved_evidence(
                                        session,
                                        execution=execution,
                                        step=step,
                                        tool_policy=authz.tool_policy,
                                        approval_policy=authz.approval_policy,
                                    )
                                except AppError as exc:
                                    pre_send_failure = _PreSendFailClosed(
                                        error_code=exc.code, message=exc.message
                                    )
                        if pre_send_failure is None and authz.confirmation_required:
                            await _assert_confirmation_evidence(
                                session, execution, authz.policy_snapshot
                            )
                    except AppError as exc:
                        pre_send_failure = _PreSendFailClosed(
                            error_code=exc.code, message=exc.message
                        )

                if pre_send_failure is None and (
                    server.protocol_era != MCPProtocolEra.CURRENT.value
                    or server.transport_type != MCPTransportType.STREAMABLE_HTTP.value
                ):
                    pre_send_failure = _PreSendFailClosed(
                        error_code="MCP_TRANSPORT_UNSUPPORTED",
                        message=(
                            "MCP Tool Runner supports CURRENT + STREAMABLE_HTTP"
                            " servers only."
                        ),
                    )
                if pre_send_failure is None and not server.endpoint_url:
                    pre_send_failure = _PreSendFailClosed(
                        error_code="MCP_ENDPOINT_MISSING",
                        message=(
                            "MCP Server endpoint_url is required for STREAMABLE_HTTP"
                            " tools/call."
                        ),
                    )

                remote_request_id = str(uuid.uuid4())
                call_timeout_ms = remaining_step_timeout_ms(
                    step_started_at=step.started_at,
                    timeout_seconds=_plan_timeout_seconds(step),
                    policy_timeout_ms=tool_policy.timeout_ms,
                    now=now,
                )
                if call_timeout_ms < 1:
                    if pre_send_failure is None:
                        pre_send_failure = _PreSendFailClosed(
                            error_code="STEP_TIMEOUT_EXCEEDED",
                            message=(
                                "Step total timeout budget exhausted before tools/call."
                            ),
                        )
                    call_timeout_ms = 1
                request_meta: dict[str, Any] = {
                    "method": "tools/call",
                    "tool_name": logical_tool.remote_name,
                    "timeout_ms": call_timeout_ms,
                    "auth_type": server.auth_type,
                    "content_type": "application/json",
                }
                if mrtr_resume is not None:
                    # Never persist requestState / response values in request_meta.
                    request_meta["mrtr_resume"] = True
                    request_meta["mrtr_input_request_id"] = str(mrtr_resume[0])
                tool_call = await executions.create_tool_call(
                    step_attempt_id=attempt.id,
                    mcp_server_id=server.id,
                    mcp_tool_version_id=tool_version.id,
                    protocol_era=server.protocol_era,
                    protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
                    transport_type=server.transport_type,
                    remote_request_id=remote_request_id,
                    request_meta=request_meta,
                    normalized_status=ToolCallNormalizedStatus.STARTED.value,
                    started_at=now,
                )

                if pre_send_failure is not None:
                    terminal, disposition = _apply_terminal_transition(
                        execution=execution,
                        step=step,
                        attempt=attempt,
                        tool_call=tool_call,
                        call_error=pre_send_failure,
                        result=None,
                        response_meta=None,
                        first_byte_at=None,
                        risk_class=tool_policy.risk_class,
                        output_schema=tool_version.output_schema,
                        result_inline_max_bytes=self._result_inline_max_bytes,
                        now=now,
                        defer_execution_terminalization=defer,
                    )
                    return _PrepareResult(
                        outcome=ToolRunOutcome(
                            execution_id=execution.id,
                            step_execution_id=step.id,
                            attempt_id=attempt.id,
                            tool_call_id=tool_call.id,
                            mcp_called=False,
                            terminal_status=terminal,
                            reason=pre_send_failure.error_code,
                            disposition=disposition,
                        )
                    )

                prepared = _PreparedCall(
                    execution_id=execution.id,
                    step_id=step.id,
                    attempt_id=attempt.id,
                    tool_call_id=tool_call.id,
                    remote_request_id=tool_call.remote_request_id,
                    mcp_server_id=server.id,
                    mcp_tool_version_id=tool_version.id,
                    endpoint=str(server.endpoint_url),
                    tool_name=logical_tool.remote_name,
                    timeout_ms=call_timeout_ms,
                    policy_timeout_ms=tool_policy.timeout_ms,
                    resolved_input=dict(step.resolved_input or {}),
                    auth_type=server.auth_type,
                    auth_secret_id=server.auth_secret_id,
                    protocol_era=server.protocol_era,
                    transport_type=server.transport_type,
                    risk_class=tool_policy.risk_class,
                    output_schema=tool_version.output_schema,
                    max_result_bytes=tool_policy.max_result_bytes,
                    worker_id=worker,
                    lease_token=lease_token,
                    mrtr_input_request_id=(
                        mrtr_resume[0] if mrtr_resume is not None else None
                    ),
                    mrtr_input_responses=(
                        dict(mrtr_resume[1]) if mrtr_resume is not None else None
                    ),
                    mrtr_request_state=(
                        mrtr_resume[2] if mrtr_resume is not None else None
                    ),
                    defer_execution_terminalization=defer,
                    forbid_execution_wait=forbid_wait,
                )
                return _PrepareResult(prepared=prepared)

    def _noop(
        self,
        execution_id: uuid.UUID,
        reason: str,
        *,
        step_id: uuid.UUID | None = None,
        status: str | None = None,
    ) -> ToolRunOutcome:
        return ToolRunOutcome(
            execution_id=execution_id,
            step_execution_id=step_id,
            attempt_id=None,
            tool_call_id=None,
            mcp_called=False,
            terminal_status=status,
            reason=reason,
        )

    # -- Phase B -------------------------------------------------------------

    async def _resolve_secrets(
        self, prepared: _PreparedCall
    ) -> tuple[
        dict[str, str],
        dict[str, Any],
        dict[str, Any] | None,
        tuple[str, ...],
    ]:
        needs_secret = (
            prepared.auth_type != MCPAuthType.NONE.value
            or _resolved_input_needs_secret(prepared.resolved_input)
            or _payload_needs_secret(prepared.mrtr_input_responses)
        )
        auth_material_values: list[str] = []
        secret_argument_values: list[str] = []
        async with self._session_factory() as session:
            # NONE + no SECRET_REF must not force master-key loading. Resolvers
            # that lazy-load on first resolve() stay idle on this path.
            if needs_secret:
                resolver = self._secret_resolver_factory(session)
            else:
                resolver = _NoSecretResolver()
            auth_headers: dict[str, str] = {}
            if prepared.auth_type != MCPAuthType.NONE.value:
                resolved = (
                    await resolver.resolve(prepared.auth_secret_id)
                    if prepared.auth_secret_id is not None
                    else None
                )
                auth_material_values.extend(_auth_material_plaintexts(resolved))
                auth_headers = build_mcp_auth_headers(
                    auth_type=prepared.auth_type, resolved=resolved
                )
            arguments = await materialize_tool_arguments(
                prepared.resolved_input, secret_resolver=resolver
            )
            for key, raw in prepared.resolved_input.items():
                if (
                    isinstance(raw, dict)
                    and raw.get("kind") == BindingKind.SECRET_REF.value
                ):
                    materialized = arguments.get(key)
                    if isinstance(materialized, str) and materialized:
                        secret_argument_values.append(materialized)
            materialized_responses: dict[str, Any] | None = None
            if prepared.mrtr_input_responses is not None:
                # Same SECRET_REF boundary as tool arguments — reference-only
                # durable payload → memory-only plaintext for the MCP round.
                materialized_responses = await materialize_tool_arguments(
                    prepared.mrtr_input_responses, secret_resolver=resolver
                )
                for key, raw in prepared.mrtr_input_responses.items():
                    if (
                        isinstance(raw, dict)
                        and raw.get("kind") == BindingKind.SECRET_REF.value
                    ):
                        materialized = materialized_responses.get(key)
                        if isinstance(materialized, str) and materialized:
                            secret_argument_values.append(materialized)
        protected = collect_protected_plaintexts(
            auth_headers=auth_headers,
            secret_argument_values=secret_argument_values,
            auth_material_values=auth_material_values,
        )
        return auth_headers, arguments, materialized_responses, protected

    async def _heartbeat_loop(self, prepared: _PreparedCall) -> None:
        interval = max(_MIN_HEARTBEAT_INTERVAL_SECONDS, self._lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        await ExecutionClaimService(
                            session, lease_seconds=self._lease_seconds
                        ).renew_lease(
                            execution_id=prepared.execution_id,
                            worker_id=prepared.worker_id,
                            lease_token=prepared.lease_token,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "lease heartbeat renewal failed execution_id=%s",
                    prepared.execution_id,
                    exc_info=True,
                )

    async def _call_remote(
        self,
        prepared: _PreparedCall,
        auth_headers: dict[str, str],
        arguments: dict[str, Any],
        input_responses: dict[str, Any] | None = None,
    ) -> tuple[
        NormalizedToolResult | NormalizedInputRequired | None,
        dict[str, Any] | None,
        datetime | None,
        MCPClientError | None,
    ]:
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(prepared))
        try:
            include_mrtr = prepared.mrtr_input_request_id is not None
            result, response_meta, first_byte_at = await self._mcp_client.call_tool(
                prepared.endpoint,
                tool_name=prepared.tool_name,
                arguments=arguments,
                timeout_ms=prepared.timeout_ms,
                remote_request_id=prepared.remote_request_id,
                auth_headers=auth_headers,
                max_result_bytes=prepared.max_result_bytes,
                input_responses=input_responses if include_mrtr else None,
                request_state=prepared.mrtr_request_state if include_mrtr else None,
                include_mrtr_resume=include_mrtr,
            )
            return result, response_meta, first_byte_at, None
        except MCPClientError as exc:
            return None, None, None, exc
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            auth_headers.clear()
            arguments.clear()
            if input_responses is not None:
                input_responses.clear()

    # -- Phase C -------------------------------------------------------------

    async def _finalize_input_required(
        self,
        prepared: _PreparedCall,
        *,
        mrtr: NormalizedInputRequired,
        response_meta: dict[str, Any] | None,
        first_byte_at: datetime | None,
        protected: tuple[str, ...] = (),
    ) -> ToolRunOutcome:
        """Fenced Phase C for valid MRTR input_required — not a Tool failure."""
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                inputs = MCPInputRequestRepository(session)
                execution = await executions.lock_execution(prepared.execution_id)
                step = await executions.lock_step(prepared.step_id)
                attempt = await executions.get_attempt_with_lock(prepared.attempt_id)
                tool_call = await executions.get_tool_call_with_lock(prepared.tool_call_id)

                now = datetime.now(UTC)
                if (
                    execution is None
                    or step is None
                    or attempt is None
                    or tool_call is None
                    or execution.status != ExecutionStatus.RUNNING.value
                    or execution.worker_id != prepared.worker_id
                    or execution.lease_token != prepared.lease_token
                    or execution.lease_expires_at is None
                    or _as_utc(execution.lease_expires_at) <= _as_utc(now)
                    or step.execution_id != execution.id
                    or attempt.step_execution_id != step.id
                    or tool_call.step_attempt_id != attempt.id
                    or tool_call.remote_request_id != prepared.remote_request_id
                    or tool_call.mcp_server_id != prepared.mcp_server_id
                    or tool_call.mcp_tool_version_id != prepared.mcp_tool_version_id
                ):
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Tool Runner MRTR finalize fencing failed.",
                        status_code=409,
                    )

                timeout_seconds = _plan_timeout_seconds(step)
                persist_meta = sanitize_for_persistence(response_meta, protected) or {}
                if not isinstance(persist_meta, dict):
                    persist_meta = {}
                # Safe transport metadata only — never requestState / inputRequests dump.
                persist_meta = {
                    **persist_meta,
                    "result_type": "input_required",
                }
                # Strip accidental leakage if present.
                persist_meta.pop("requestState", None)
                persist_meta.pop("request_state", None)
                persist_meta.pop("inputRequests", None)
                persist_meta.pop("input_requests", None)

                # requestState/inputRequests must stay exact for resume — never
                # redact in place. Echoed invocation secrets fail closed.
                if contains_protected_plaintext(
                    mrtr.input_requests, protected
                ) or contains_protected_plaintext(mrtr.request_state, protected):
                    return _terminalize_mrtr_secret_echo(
                        execution=execution,
                        step=step,
                        attempt=attempt,
                        tool_call=tool_call,
                        persist_meta=persist_meta,
                        response_bytes=mrtr.raw_size_bytes,
                        first_byte_at=first_byte_at,
                        now=now,
                        defer_execution_terminalization=(
                            prepared.defer_execution_terminalization
                        ),
                    )

                if step_timeout_budget_exhausted(
                    step_started_at=step.started_at,
                    timeout_seconds=timeout_seconds,
                    now=now,
                ):
                    # Persist EXPIRED evidence; no OPEN wait / no WAITING_INPUT.
                    expires_at = now
                    if step.started_at is not None and timeout_seconds is not None:
                        expires_at = _as_utc(step.started_at) + timedelta(
                            seconds=int(timeout_seconds)
                        )
                    # Count this round as SUCCEEDED before computing round_no.
                    prior_succeeded = [
                        tc
                        for tc in await executions.list_tool_calls(attempt.id)
                        if tc.id != tool_call.id
                        and tc.normalized_status
                        == ToolCallNormalizedStatus.SUCCEEDED.value
                    ]
                    round_no = len(prior_succeeded) + 1
                    await inputs.create(
                        execution_id=execution.id,
                        step_execution_id=step.id,
                        step_attempt_id=attempt.id,
                        protocol_era=MCPProtocolEra.CURRENT.value,
                        input_requests=dict(mrtr.input_requests),
                        request_state=mrtr.request_state,
                        round_no=round_no,
                        status=McpInputRequestStatus.EXPIRED.value,
                        requested_at=now,
                        expires_at=expires_at,
                    )
                    tool_call.normalized_status = ToolCallNormalizedStatus.SUCCEEDED.value
                    tool_call.response_meta = persist_meta
                    tool_call.response_bytes = mrtr.raw_size_bytes
                    tool_call.first_byte_at = first_byte_at
                    tool_call.finished_at = now

                    attempt.status = StepAttemptStatus.TIMED_OUT.value
                    attempt.error_layer = "PROTOCOL"
                    attempt.error_code = "STEP_TIMEOUT"
                    attempt.error_message = (
                        "Step total timeout exhausted before MRTR wait could open."
                    )
                    attempt.is_retryable = False
                    attempt.finished_at = now
                    attempt.worker_id = None
                    attempt.lease_expires_at = None

                    step.status = StepStatus.TIMED_OUT.value
                    step.error_code = "STEP_TIMEOUT"
                    step.error_message = attempt.error_message
                    step.finished_at = now
                    step.lock_version += 1

                    # Known TIMED_OUT — ErrorPolicy owned by orchestrator.
                    execution.heartbeat_at = now
                    execution.lock_version += 1

                    return ToolRunOutcome(
                        execution_id=execution.id,
                        step_execution_id=step.id,
                        attempt_id=attempt.id,
                        tool_call_id=tool_call.id,
                        mcp_called=True,
                        terminal_status=StepStatus.TIMED_OUT.value,
                        reason="STEP_TIMEOUT",
                        disposition=DISPOSITION_KNOWN_STEP_FAILURE,
                    )

                assert step.started_at is not None
                assert timeout_seconds is not None and timeout_seconds >= 1
                expires_at = _as_utc(step.started_at) + timedelta(
                    seconds=int(timeout_seconds)
                )

                prior_succeeded = [
                    tc
                    for tc in await executions.list_tool_calls(attempt.id)
                    if tc.id != tool_call.id
                    and tc.normalized_status
                    == ToolCallNormalizedStatus.SUCCEEDED.value
                ]
                round_no = len(prior_succeeded) + 1
                if round_no > MAX_MRTR_ROUNDS:
                    max_rounds_message = (
                        f"MRTR round limit ({MAX_MRTR_ROUNDS}) exceeded."
                    )
                    tool_call.normalized_status = ToolCallNormalizedStatus.FAILED.value
                    tool_call.response_meta = persist_meta
                    tool_call.response_bytes = mrtr.raw_size_bytes
                    tool_call.first_byte_at = first_byte_at
                    tool_call.finished_at = now

                    attempt.status = StepAttemptStatus.FAILED.value
                    attempt.error_layer = "PROTOCOL"
                    attempt.error_code = "MAX_MRTR_ROUNDS_EXCEEDED"
                    attempt.error_message = max_rounds_message
                    attempt.is_retryable = False
                    attempt.finished_at = now
                    attempt.worker_id = None
                    attempt.lease_expires_at = None

                    step.status = StepStatus.FAILED.value
                    step.error_code = "MAX_MRTR_ROUNDS_EXCEEDED"
                    step.error_message = max_rounds_message
                    step.finished_at = now
                    step.lock_version += 1

                    _apply_or_defer_execution_fatal(
                        execution,
                        error_code="MAX_MRTR_ROUNDS_EXCEEDED",
                        error_message=max_rounds_message,
                        now=now,
                        defer=prepared.defer_execution_terminalization,
                    )

                    return ToolRunOutcome(
                        execution_id=execution.id,
                        step_execution_id=step.id,
                        attempt_id=attempt.id,
                        tool_call_id=tool_call.id,
                        mcp_called=True,
                        terminal_status=StepStatus.FAILED.value,
                        reason="MAX_MRTR_ROUNDS_EXCEEDED",
                        disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
                    )

                # Multi-Step DAG: never open Execution-level WAITING_INPUT while
                # siblings may still be in flight. Remote call already happened —
                # classify conservatively as UNKNOWN_OUTCOME (no OPEN wait row).
                if prepared.forbid_execution_wait:
                    dag_wait_message = (
                        "Multi-Step TOOL/JOIN DAG cannot enter WAITING_INPUT; "
                        "branch-local MRTR suspension is out of scope."
                    )
                    tool_call.normalized_status = (
                        ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
                    )
                    tool_call.response_meta = persist_meta
                    tool_call.response_bytes = mrtr.raw_size_bytes
                    tool_call.first_byte_at = first_byte_at
                    tool_call.finished_at = now

                    attempt.status = StepAttemptStatus.UNKNOWN_OUTCOME.value
                    attempt.error_layer = "PROTOCOL"
                    attempt.error_code = "DAG_WAIT_UNSUPPORTED"
                    attempt.error_message = dag_wait_message
                    attempt.is_retryable = False
                    attempt.finished_at = now
                    attempt.worker_id = None
                    attempt.lease_expires_at = None

                    step.status = StepStatus.UNKNOWN_OUTCOME.value
                    step.error_code = "DAG_WAIT_UNSUPPORTED"
                    step.error_message = dag_wait_message
                    step.finished_at = now
                    step.lock_version += 1

                    _apply_or_defer_execution_fatal(
                        execution,
                        error_code="DAG_WAIT_UNSUPPORTED",
                        error_message=dag_wait_message,
                        now=now,
                        defer=prepared.defer_execution_terminalization,
                    )
                    return ToolRunOutcome(
                        execution_id=execution.id,
                        step_execution_id=step.id,
                        attempt_id=attempt.id,
                        tool_call_id=tool_call.id,
                        mcp_called=True,
                        terminal_status=StepStatus.UNKNOWN_OUTCOME.value,
                        reason="DAG_WAIT_UNSUPPORTED",
                        disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
                    )

                open_existing = await inputs.list_open_for_step(
                    execution_id=execution.id, step_execution_id=step.id
                )
                if open_existing:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Cannot open a second OPEN MCPInputRequest.",
                        status_code=409,
                    )

                # Network ToolCall round completed; logical Attempt remains STARTED.
                tool_call.normalized_status = ToolCallNormalizedStatus.SUCCEEDED.value
                tool_call.response_meta = persist_meta
                tool_call.response_bytes = mrtr.raw_size_bytes
                tool_call.first_byte_at = first_byte_at
                tool_call.finished_at = now

                attempt.worker_id = None
                attempt.lease_expires_at = None
                # STARTED, finished_at null, no error/result — unchanged status.

                step.status = StepStatus.WAITING_INPUT.value
                step.finished_at = None
                step.error_code = None
                step.error_message = None
                step.lock_version += 1

                execution.status = ExecutionStatus.WAITING_INPUT.value
                execution.finished_at = None
                execution.error_code = None
                execution.error_message = None
                execution.worker_id = None
                execution.lease_token = None
                execution.lease_expires_at = None
                execution.heartbeat_at = None
                execution.lock_version += 1

                await inputs.create(
                    execution_id=execution.id,
                    step_execution_id=step.id,
                    step_attempt_id=attempt.id,
                    protocol_era=MCPProtocolEra.CURRENT.value,
                    input_requests=dict(mrtr.input_requests),
                    request_state=mrtr.request_state,
                    round_no=round_no,
                    status=McpInputRequestStatus.OPEN.value,
                    requested_at=now,
                    expires_at=expires_at,
                )

                return ToolRunOutcome(
                    execution_id=execution.id,
                    step_execution_id=step.id,
                    attempt_id=attempt.id,
                    tool_call_id=tool_call.id,
                    mcp_called=True,
                    terminal_status=StepStatus.WAITING_INPUT.value,
                    reason=_REASON_WAITING_INPUT,
                    disposition=DISPOSITION_WAIT,
                )

    async def _finalize_locked(
        self,
        prepared: _PreparedCall,
        *,
        call_error: MCPClientError | None,
        result: NormalizedToolResult | None,
        response_meta: dict[str, Any] | None,
        first_byte_at: datetime | None,
        protected: tuple[str, ...] = (),
    ) -> ToolRunOutcome:
        async with self._session_factory() as session:
            async with session.begin():
                executions = ExecutionRepository(session)
                execution = await executions.lock_execution(prepared.execution_id)
                step = await executions.lock_step(prepared.step_id)
                attempt = await executions.get_attempt_with_lock(prepared.attempt_id)
                tool_call = await executions.get_tool_call_with_lock(prepared.tool_call_id)

                now = datetime.now(UTC)
                if (
                    execution is None
                    or step is None
                    or attempt is None
                    or tool_call is None
                    or execution.status != ExecutionStatus.RUNNING.value
                    or execution.worker_id != prepared.worker_id
                    or execution.lease_token != prepared.lease_token
                    or execution.lease_expires_at is None
                    or _as_utc(execution.lease_expires_at) <= _as_utc(now)
                    or step.execution_id != execution.id
                    or attempt.step_execution_id != step.id
                    or tool_call.step_attempt_id != attempt.id
                    or tool_call.remote_request_id != prepared.remote_request_id
                    or tool_call.mcp_server_id != prepared.mcp_server_id
                    or tool_call.mcp_tool_version_id != prepared.mcp_tool_version_id
                ):
                    # Stale/expired owner must not write terminal state.
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message="Tool Runner finalize fencing failed.",
                        status_code=409,
                    )

                plan_step_id = str(step.step_snapshot.get("id") or step.step_key)
                step_policy = get_expected_tool_policy_snapshot(
                    execution,
                    plan_step_id=plan_step_id,
                    tool_version_id=step.mcp_tool_version_id,
                )
                risk_for_classify = (
                    pinned_risk_class(step_policy) or prepared.risk_class
                )
                classified = (
                    _classify_mcp_failure(call_error, risk_class=risk_for_classify)
                    if call_error is not None
                    else StepStatus.FAILED.value
                )

                max_attempts = pinned_max_attempts(step_policy) or 1
                retry = decide_safe_transient_retry(
                    call_error=None
                    if call_error is None or isinstance(call_error, _PreSendFailClosed)
                    else call_error,
                    classified_terminal=classified,
                    risk_class=risk_for_classify,
                    attempt_count=step.attempt_count,
                    max_attempts=max_attempts,
                    backoff_policy=pinned_backoff_policy(step_policy),
                    step_started_at=step.started_at,
                    timeout_seconds=_plan_timeout_seconds(step),
                    now=now,
                )

                if retry.schedule_retry:
                    attempt_terminal = _apply_retry_checkpoint(
                        step=step,
                        attempt=attempt,
                        tool_call=tool_call,
                        call_error=call_error,
                        response_meta=response_meta,
                        first_byte_at=first_byte_at,
                        risk_class=risk_for_classify,
                        now=now,
                        protected=protected,
                    )
                    return ToolRunOutcome(
                        execution_id=execution.id,
                        step_execution_id=step.id,
                        attempt_id=attempt.id,
                        tool_call_id=tool_call.id,
                        mcp_called=True,
                        terminal_status=attempt_terminal,
                        reason=_REASON_SAFE_RETRY_READY,
                        disposition=DISPOSITION_RETRY,
                    )

                terminal, disposition = _apply_terminal_transition(
                    execution=execution,
                    step=step,
                    attempt=attempt,
                    tool_call=tool_call,
                    call_error=call_error,
                    result=result,
                    response_meta=response_meta,
                    first_byte_at=first_byte_at,
                    risk_class=risk_for_classify,
                    output_schema=prepared.output_schema,
                    result_inline_max_bytes=self._result_inline_max_bytes,
                    now=now,
                    protected=protected,
                    defer_execution_terminalization=(
                        prepared.defer_execution_terminalization
                    ),
                )
                mcp_called = not isinstance(call_error, _PreSendFailClosed)
                reason = (
                    "STEP_SUCCEEDED"
                    if terminal == StepStatus.SUCCEEDED.value
                    else terminal
                )
                return ToolRunOutcome(
                    execution_id=execution.id,
                    step_execution_id=step.id,
                    attempt_id=attempt.id,
                    tool_call_id=tool_call.id,
                    mcp_called=mcp_called,
                    terminal_status=terminal,
                    reason=reason,
                    disposition=disposition,
                )


_MRTR_SECRET_ECHO_CODE = "MCP_MRTR_SECRET_ECHO"
_MRTR_SECRET_ECHO_MESSAGE = (
    "MRTR input_required reflected protected invocation material; "
    "WAITING_INPUT was not opened."
)


def _terminalize_mrtr_secret_echo(
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    tool_call: ToolCall,
    persist_meta: dict[str, Any],
    response_bytes: int,
    first_byte_at: datetime | None,
    now: datetime,
    defer_execution_terminalization: bool = False,
) -> ToolRunOutcome:
    """Fail closed when MRTR payload embeds protected plaintext.

    Creates no MCPInputRequest and never enters WAITING_INPUT. Does not
    persist or log the offending secret value.
    """
    tool_call.normalized_status = ToolCallNormalizedStatus.FAILED.value
    tool_call.response_meta = persist_meta
    tool_call.response_bytes = response_bytes
    tool_call.first_byte_at = first_byte_at
    tool_call.finished_at = now

    attempt.status = StepAttemptStatus.FAILED.value
    attempt.error_layer = "PROTOCOL"
    attempt.error_code = _MRTR_SECRET_ECHO_CODE
    attempt.error_message = _MRTR_SECRET_ECHO_MESSAGE
    attempt.is_retryable = False
    attempt.result_inline = None
    attempt.finished_at = now
    attempt.worker_id = None
    attempt.lease_expires_at = None

    step.status = StepStatus.FAILED.value
    step.error_code = _MRTR_SECRET_ECHO_CODE
    step.error_message = _MRTR_SECRET_ECHO_MESSAGE
    step.result_inline = None
    step.finished_at = now
    step.lock_version += 1

    _apply_or_defer_execution_fatal(
        execution,
        error_code=_MRTR_SECRET_ECHO_CODE,
        error_message=_MRTR_SECRET_ECHO_MESSAGE,
        now=now,
        defer=defer_execution_terminalization,
    )

    return ToolRunOutcome(
        execution_id=execution.id,
        step_execution_id=step.id,
        attempt_id=attempt.id,
        tool_call_id=tool_call.id,
        mcp_called=True,
        terminal_status=StepStatus.FAILED.value,
        reason=_MRTR_SECRET_ECHO_CODE,
        disposition=DISPOSITION_FATAL_EXECUTION_FAILURE,
    )


def _apply_retry_checkpoint(
    *,
    step: ExecutionStep,
    attempt: StepAttempt,
    tool_call: ToolCall,
    call_error: MCPClientError | None,
    response_meta: dict[str, Any] | None,
    first_byte_at: datetime | None,
    risk_class: str,
    now: datetime,
    protected: tuple[str, ...] = (),
) -> str:
    """Terminalize the current Attempt/ToolCall and return Step to READY.

    Compatible with Recovery SAFE_RETRY crash-gap checkpoint: historical
    terminal Attempt + READY Step + RUNNING Execution (lease unchanged).
    """
    assert call_error is not None
    terminal = _classify_mcp_failure(call_error, risk_class=risk_class)
    persist_meta = sanitize_for_persistence(response_meta, protected)
    error_message = redact_text(call_error.message, protected)

    tool_call.normalized_status = terminal
    tool_call.response_meta = persist_meta
    tool_call.response_bytes = None
    tool_call.first_byte_at = first_byte_at
    tool_call.finished_at = now

    attempt.status = terminal
    attempt.error_layer = call_error.error_layer
    attempt.error_code = call_error.error_code
    attempt.error_message = error_message
    attempt.is_retryable = True
    attempt.result_inline = None
    attempt.finished_at = now

    # Clear only fields that would make READY look terminal; keep started_at /
    # attempt_count / ready_at history.
    step.status = StepStatus.READY.value
    step.error_code = None
    step.error_message = None
    step.result_inline = None
    step.finished_at = None
    step.lock_version += 1
    return terminal


def _apply_terminal_transition(
    *,
    execution: Execution,
    step: ExecutionStep,
    attempt: StepAttempt,
    tool_call: ToolCall,
    call_error: MCPClientError | None,
    result: NormalizedToolResult | None,
    response_meta: dict[str, Any] | None,
    first_byte_at: datetime | None,
    risk_class: str,
    output_schema: Any,
    result_inline_max_bytes: int,
    now: datetime,
    protected: tuple[str, ...] = (),
    defer_execution_terminalization: bool = False,
) -> tuple[str, str]:
    """Terminalize ToolCall/Attempt/Step; Execution only for fatal dispositions.

    Returns ``(step_terminal_status, disposition)``. Ordinary known TOOL
    failures leave Execution RUNNING under the same lease so
    ``ExecutionOrchestrator`` can apply ErrorPolicy / ALL_REQUIRED.

    When ``defer_execution_terminalization`` is set (DAG wave mode), mandatory
    fatal dispositions still return ``FATAL_EXECUTION_FAILURE`` but keep
    Execution RUNNING with the lease so already-dispatched siblings can settle.

    Protocol/schema validation uses the in-memory remote ``result``. Persistence
    fields are written from redacted copies when ``protected`` is non-empty.
    """
    response_bytes: int | None = None
    persist_meta = sanitize_for_persistence(response_meta, protected)

    if call_error is not None:
        if (
            isinstance(call_error, _PreSendFailClosed)
            and call_error.error_code == "STEP_TIMEOUT_EXCEEDED"
        ):
            terminal = StepStatus.TIMED_OUT.value
        else:
            terminal = _classify_mcp_failure(call_error, risk_class=risk_class)
        error_layer = call_error.error_layer
        error_code = call_error.error_code
        error_message = redact_text(call_error.message, protected)
        result_inline: dict[str, Any] | None = None
    else:
        assert result is not None
        validation = validate_tool_result(result, output_schema=output_schema)
        response_bytes = result.raw_size_bytes
        if validation.ok:
            serialized = _serialize_result_inline(result)
            size = len(json.dumps(serialized, default=str, ensure_ascii=False).encode("utf-8"))
            if size > result_inline_max_bytes:
                terminal = StepStatus.FAILED.value
                error_layer = "PROTOCOL"
                error_code = "RESULT_TOO_LARGE_FOR_INLINE"
                error_message = "Tool result exceeds inline persistence size limit."
                result_inline = None
            else:
                terminal = StepStatus.SUCCEEDED.value
                error_layer = None
                error_code = None
                error_message = None
                result_inline = sanitize_for_persistence(serialized, protected)
        else:
            terminal = StepStatus.FAILED.value
            error_layer = "TOOL" if validation.error_code == "RESULT_TOOL_ERROR" else "PROTOCOL"
            error_code = validation.error_code
            error_message = redact_text(validation.error_message or "", protected) or None
            result_inline = None

    finished_at = now

    tool_call.normalized_status = terminal
    tool_call.response_meta = persist_meta
    tool_call.response_bytes = response_bytes
    tool_call.first_byte_at = first_byte_at
    tool_call.finished_at = finished_at

    attempt.status = terminal
    attempt.error_layer = error_layer
    attempt.error_code = error_code
    attempt.error_message = error_message
    attempt.is_retryable = bool(call_error.retryable) if call_error is not None else False
    attempt.result_inline = result_inline
    attempt.finished_at = finished_at

    step.status = terminal
    step.error_code = error_code
    step.error_message = error_message
    step.result_inline = result_inline
    step.finished_at = finished_at
    step.lock_version += 1

    if terminal == StepStatus.SUCCEEDED.value:
        # Step success is not Execution success when unfinished Steps remain.
        execution.heartbeat_at = finished_at
        execution.lock_version += 1
        return terminal, DISPOSITION_SUCCESS

    # Mandatory-fatal: UNKNOWN_OUTCOME and pre-send security/integrity fail-closed
    # (except Step total timeout, which is a known TIMED_OUT).
    pre_send_fatal = isinstance(call_error, _PreSendFailClosed) and (
        call_error.error_code != "STEP_TIMEOUT_EXCEEDED"
    )
    if terminal == StepStatus.UNKNOWN_OUTCOME.value or pre_send_fatal:
        fatal_message = (
            "MCP tool outcome is unknown after a possible external side effect;"
            " it is not automatically retried."
            if terminal == StepStatus.UNKNOWN_OUTCOME.value
            else error_message
        )
        _apply_or_defer_execution_fatal(
            execution,
            error_code=error_code,
            error_message=fatal_message,
            now=finished_at,
            defer=defer_execution_terminalization,
        )
        return terminal, DISPOSITION_FATAL_EXECUTION_FAILURE

    # Ordinary known TOOL failure/timeout — keep RUNNING + lease for orchestrator.
    execution.heartbeat_at = finished_at
    execution.lock_version += 1
    return terminal, DISPOSITION_KNOWN_STEP_FAILURE
