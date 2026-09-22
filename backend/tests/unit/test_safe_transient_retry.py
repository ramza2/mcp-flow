"""Bounded safe MCP retry — decision helper + Tool Runner regressions (FNC-EXE-006)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.agent.plan_validator import PlanValidatorService
from app.domain.enums import (
    ExecutionStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.retry_decision import (
    decide_safe_transient_retry,
    is_normal_auto_retry_risk,
    remaining_step_timeout_ms,
    step_timeout_budget_exhausted,
)
from app.execution.tool_runner import McpToolRunner, _PreparedCall
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError, MCPResultTooLargeError
from app.models.auth import ResourceGrant
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import (
    _create,
    _idem_key,
    _install_no_side_effects,
)
from tests.unit.test_plan_validator import _seed_validating
from tests.unit.test_tool_runner_security import _unimplemented_resolver_factory


# ---------------------------------------------------------------------------
# Decision helper
# ---------------------------------------------------------------------------


def test_normal_auto_retry_risk_is_read_only_only() -> None:
    assert is_normal_auto_retry_risk(RiskClass.READ_ONLY.value) is True
    assert is_normal_auto_retry_risk(RiskClass.IDEMPOTENT_WRITE.value) is False
    assert is_normal_auto_retry_risk(RiskClass.NON_IDEMPOTENT_WRITE.value) is False


def test_decide_retry_read_only_transient() -> None:
    err = MCPClientError(
        error_layer="NETWORK",
        error_code="MCP_NETWORK_ERROR",
        message="connect failed",
        retryable=True,
        outcome_unknown=False,
    )
    decision = decide_safe_transient_retry(
        call_error=err,
        classified_terminal=StepStatus.FAILED.value,
        risk_class=RiskClass.READ_ONLY.value,
        attempt_count=1,
        max_attempts=2,
        backoff_policy=None,
        step_started_at=datetime.now(UTC),
        timeout_seconds=30,
    )
    assert decision.schedule_retry is True


def test_decide_retry_rejects_unknown_outcome_and_unsafe() -> None:
    err = MCPClientError(
        error_layer="TIMEOUT",
        error_code="MCP_CONNECTION_TIMEOUT",
        message="read timeout",
        retryable=True,
        outcome_unknown=True,
    )
    assert (
        decide_safe_transient_retry(
            call_error=err,
            classified_terminal=StepStatus.UNKNOWN_OUTCOME.value,
            risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
            attempt_count=1,
            max_attempts=3,
            backoff_policy=None,
            step_started_at=datetime.now(UTC),
            timeout_seconds=30,
        ).schedule_retry
        is False
    )


def test_decide_retry_rejects_non_null_backoff_and_exhausted() -> None:
    err = MCPClientError(
        error_layer="NETWORK",
        error_code="MCP_NETWORK_ERROR",
        message="x",
        retryable=True,
        outcome_unknown=False,
    )
    assert (
        decide_safe_transient_retry(
            call_error=err,
            classified_terminal=StepStatus.FAILED.value,
            risk_class=RiskClass.READ_ONLY.value,
            attempt_count=1,
            max_attempts=2,
            backoff_policy={"initial_ms": 10},
            step_started_at=datetime.now(UTC),
            timeout_seconds=30,
        ).reason
        == "BACKOFF_POLICY_UNSUPPORTED"
    )
    assert (
        decide_safe_transient_retry(
            call_error=err,
            classified_terminal=StepStatus.FAILED.value,
            risk_class=RiskClass.READ_ONLY.value,
            attempt_count=2,
            max_attempts=2,
            backoff_policy=None,
            step_started_at=datetime.now(UTC),
            timeout_seconds=30,
        ).schedule_retry
        is False
    )


def test_step_timeout_budget_helpers() -> None:
    started = datetime.now(UTC) - timedelta(seconds=30)
    assert (
        step_timeout_budget_exhausted(
            step_started_at=started, timeout_seconds=30, now=datetime.now(UTC)
        )
        is True
    )
    assert (
        remaining_step_timeout_ms(
            step_started_at=datetime.now(UTC),
            timeout_seconds=30,
            policy_timeout_ms=60_000,
            now=datetime.now(UTC),
        )
        <= 60_000
    )


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


class _FailThenSucceedClient:
    def __init__(self, *, first_error: MCPClientError) -> None:
        self.calls = 0
        self._first_error = first_error

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        if self.calls == 1:
            raise self._first_error
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "ok"}],
            structured_content={"ok": True},
            raw_size_bytes=8,
            duration_ms=1,
        )
        return result, {"http_status": 200}, datetime.now(UTC)


class _AlwaysFailClient:
    def __init__(self, *, error: MCPClientError) -> None:
        self.calls = 0
        self._error = error

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        raise self._error


class _CountingSuccessClient:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "ok"}],
            raw_size_bytes=4,
            duration_ms=1,
        )
        return result, {"http_status": 200}, datetime.now(UTC)


_CONNECT_ERR = MCPClientError(
    error_layer="NETWORK",
    error_code="MCP_NETWORK_ERROR",
    message="Failed to connect to MCP server.",
    retryable=True,
    outcome_unknown=False,
)
_READ_TIMEOUT_ERR = MCPClientError(
    error_layer="TIMEOUT",
    error_code="MCP_CONNECTION_TIMEOUT",
    message="MCP server connection timed out.",
    retryable=True,
    outcome_unknown=True,
)


async def _claim_with_policy(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int = 2,
    risk_class: str = RiskClass.READ_ONLY.value,
    timeout_ms: int | None = None,
    backoff_policy: dict[str, Any] | None = None,
    worker_id: str = "worker-retry",
) -> tuple[uuid.UUID, str, uuid.UUID]:
    async with session_factory() as session:
        seeded = await _seed_validating(session)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.risk_class = risk_class
        policy.max_attempts = max_attempts
        if timeout_ms is not None:
            policy.timeout_ms = timeout_ms
        if backoff_policy is not None:
            policy.backoff_policy = backoff_policy
        await session.commit()
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "READY"
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        seeded = {**seeded, "requester_id": request.requester_id}
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim.claimed is True
        assert claim.lease_token is not None
        await session.commit()
        return execution_id, worker_id, claim.lease_token


def _runner(
    session_factory: async_sessionmaker[AsyncSession], client: Any
) -> McpToolRunner:
    return McpToolRunner(
        session_factory=session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )


# ---------------------------------------------------------------------------
# A / B success paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_transient_then_success_retries_second_attempt(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=2
    )
    client = _FailThenSucceedClient(first_error=_CONNECT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 2
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 2
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert [a.attempt_no for a in attempts] == [1, 2]
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[0].is_retryable is True
        assert attempts[0].error_code == "MCP_NETWORK_ERROR"
        assert attempts[1].status == StepAttemptStatus.SUCCEEDED.value
        tc1 = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        tc2 = await ExecutionRepository(session).list_tool_calls(attempts[1].id)
        assert tc1[0].normalized_status == ToolCallNormalizedStatus.FAILED.value
        assert tc2[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_read_only_post_send_timeout_then_success(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=2
    )
    client = _FailThenSucceedClient(first_error=_READ_TIMEOUT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 2
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.TIMED_OUT.value
        assert attempts[0].error_code == "MCP_CONNECTION_TIMEOUT"
        assert attempts[0].is_retryable is True


# ---------------------------------------------------------------------------
# C / D exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_attempts_1_no_second_call(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=1
    )
    client = _AlwaysFailClient(error=_CONNECT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 1
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1


@pytest.mark.asyncio
async def test_exhaustion_two_transient_failures(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=2
    )
    client = _AlwaysFailClient(error=_CONNECT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 2
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 2
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 2
        assert all(a.status == StepAttemptStatus.FAILED.value for a in attempts)


# ---------------------------------------------------------------------------
# E / F / G / H no-retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsafe_post_send_ambiguous_no_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory,
        max_attempts=3,
        risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
    )
    client = _AlwaysFailClient(error=_READ_TIMEOUT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.UNKNOWN_OUTCOME.value
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 1


@pytest.mark.asyncio
async def test_unsafe_pre_send_transient_no_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory,
        max_attempts=3,
        risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
    )
    client = _AlwaysFailClient(error=_CONNECT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 1


@pytest.mark.asyncio
async def test_mcp_result_too_large_no_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=3
    )

    class _TooLarge:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise MCPResultTooLargeError(max_result_bytes=10)

    client = _TooLarge()
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        assert step.attempt_count == 1
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[0].error_code == "MCP_RESULT_TOO_LARGE"
        assert attempts[0].is_retryable is False
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.FAILED.value


@pytest.mark.asyncio
async def test_tool_error_is_error_no_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=3
    )

    class _ToolError:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, *args: Any, **kwargs: Any):
            self.calls += 1
            result = NormalizedToolResult(
                protocol_success=True,
                tool_error=True,
                content=[{"type": "text", "text": "biz fail"}],
                raw_size_bytes=4,
                duration_ms=1,
            )
            return result, {"http_status": 200}, datetime.now(UTC)

    client = _ToolError()
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.FAILED.value


# ---------------------------------------------------------------------------
# I authz revoke between attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authz_revoke_between_attempts_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=2
    )
    client = _FailThenSucceedClient(first_error=_CONNECT_ERR)
    runner = _runner(db_session_factory, client)

    revoked = {"done": False}

    async def mutate(prepared: _PreparedCall) -> None:
        if revoked["done"]:
            return
        # After Attempt 1 checkpoint, Attempt 2 Phase A starts then seam runs.
        # Revoke on the second prepare (second time seam is entered).
        async with db_session_factory() as session:
            step = (await ExecutionRepository(session).list_steps(execution_id))[0]
            attempts = await ExecutionRepository(session).list_attempts(step.id)
            if len(attempts) < 2:
                return
            execution = await ExecutionRepository(session).get(execution_id)
            assert execution is not None
            grants = (
                await session.execute(
                    select(ResourceGrant).where(
                        ResourceGrant.user_id == execution.requester_id
                    )
                )
            ).scalars().all()
            for grant in grants:
                await session.delete(grant)
            await session.commit()
            revoked["done"] = True

    # Use after_phase_a seam: revoke only once Attempt 2 has started.
    call_count = {"n": 0}

    async def seam(prepared: _PreparedCall) -> None:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            await mutate(prepared)

    monkeypatch.setattr(runner, "_after_phase_a_before_final_gate", seam)

    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.mcp_called is False or outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 2
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[1].status == StepAttemptStatus.FAILED.value
        assert step.status == StepStatus.FAILED.value
        for attempt in attempts:
            tcs = await ExecutionRepository(session).list_tool_calls(attempt.id)
            assert all(
                tc.normalized_status != ToolCallNormalizedStatus.STARTED.value
                for tc in tcs
            )


# ---------------------------------------------------------------------------
# L total step timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_timeout_budget_blocks_second_call(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=3
    )

    client = _AlwaysFailClient(error=_CONNECT_ERR)
    runner = _runner(db_session_factory, client)

    original_finalize = runner._finalize_locked

    async def finalize_and_expire(*args: Any, **kwargs: Any):
        outcome = await original_finalize(*args, **kwargs)
        if outcome.reason == "SAFE_RETRY_READY":
            async with db_session_factory() as session:
                step = (await ExecutionRepository(session).list_steps(execution_id))[0]
                # Exhaust total Step budget before Attempt 2 prepare.
                step.started_at = datetime.now(UTC) - timedelta(seconds=120)
                await session.commit()
        return outcome

    monkeypatch.setattr(runner, "_finalize_locked", finalize_and_expire)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.TIMED_OUT.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.started_at is not None
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1


# ---------------------------------------------------------------------------
# N duplicate re-entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_runner_after_success_no_extra_attempt(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=2
    )
    client = _CountingSuccessClient()
    runner = _runner(db_session_factory, client)
    first = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert first.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls == 1
    second = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert second.mcp_called is False
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Safety regressions: IDEMPOTENT_WRITE / backoff / lease / timeout clamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_write_normal_path_no_auto_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IDEMPOTENT_WRITE must not auto-retry on the normal Runner path.

    StepAttempt.idempotency_key is lineage/dedup only — not remote MCP
    side-effect idempotency.
    """
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory,
        max_attempts=3,
        risk_class=RiskClass.IDEMPOTENT_WRITE.value,
    )
    client = _FailThenSucceedClient(first_error=_CONNECT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        assert step.attempt_count == 1
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1


@pytest.mark.asyncio
async def test_non_null_backoff_policy_blocks_runner_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned non-null backoff_policy must refuse auto-retry (no silent ignore)."""
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory,
        max_attempts=3,
        backoff_policy={"initial_ms": 25},
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        tool_policy = (execution.policy_snapshot or {}).get("tool_policy") or {}
        assert tool_policy.get("backoff_policy") == {"initial_ms": 25}

    client = _FailThenSucceedClient(first_error=_CONNECT_ERR)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 1
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert attempts[0].error_code == "MCP_NETWORK_ERROR"


@pytest.mark.asyncio
async def test_lease_lost_between_retry_attempts_no_second_call(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After SAFE_RETRY checkpoint, expired lease must stop Attempt 2 via prepare fencing."""
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=3
    )
    client = _FailThenSucceedClient(first_error=_CONNECT_ERR)
    runner = _runner(db_session_factory, client)
    original_finalize = runner._finalize_locked

    async def expire_lease_after_checkpoint(*args: Any, **kwargs: Any):
        outcome = await original_finalize(*args, **kwargs)
        if outcome.reason == "SAFE_RETRY_READY":
            async with db_session_factory() as session:
                execution = await ExecutionRepository(session).get(execution_id)
                assert execution is not None
                # Deterministic ownership loss before Attempt 2 prepare.
                execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
        return outcome

    monkeypatch.setattr(runner, "_finalize_locked", expire_lease_after_checkpoint)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 1
    assert outcome.mcp_called is False
    assert outcome.reason == "LEASE_MISMATCH"
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        # Stale worker must not terminalize / overwrite after lease loss.
        assert execution.status == ExecutionStatus.RUNNING.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.READY.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status != ToolCallNormalizedStatus.STARTED.value
        # No stranded Attempt 2 / STARTED ToolCall.
        started = [
            a
            for a in attempts
            if a.status == StepAttemptStatus.STARTED.value
        ]
        assert started == []


@pytest.mark.asyncio
async def test_second_call_timeout_ms_clamped_to_remaining_budget(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second tools/call must receive remaining Step budget, not full policy timeout."""
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=3, timeout_ms=30_000
    )

    class _TimeoutRecordingClient:
        def __init__(self) -> None:
            self.timeouts: list[int] = []
            self.calls = 0

        async def call_tool(self, endpoint: str, **kwargs: Any):
            self.calls += 1
            self.timeouts.append(int(kwargs["timeout_ms"]))
            if self.calls == 1:
                raise _CONNECT_ERR
            result = NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                content=[{"type": "text", "text": "ok"}],
                raw_size_bytes=4,
                duration_ms=1,
            )
            return result, {"http_status": 200}, datetime.now(UTC)

    client = _TimeoutRecordingClient()
    runner = _runner(db_session_factory, client)
    original_finalize = runner._finalize_locked
    started_at_before: dict[str, datetime | None] = {"value": None}

    async def age_step_after_checkpoint(*args: Any, **kwargs: Any):
        outcome = await original_finalize(*args, **kwargs)
        if outcome.reason == "SAFE_RETRY_READY":
            async with db_session_factory() as session:
                step = (await ExecutionRepository(session).list_steps(execution_id))[0]
                started_at_before["value"] = step.started_at
                # Deterministic elapsed time: 10s into a 30s Step budget.
                assert step.started_at is not None
                step.started_at = datetime.now(UTC) - timedelta(seconds=10)
                # Preserve the original started_at for assertion after run by
                # recording the aged value as the canonical one we expect unchanged.
                started_at_before["value"] = step.started_at
                await session.commit()
        return outcome

    monkeypatch.setattr(runner, "_finalize_locked", age_step_after_checkpoint)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls == 2
    assert len(client.timeouts) == 2
    first_ms, second_ms = client.timeouts
    assert first_ms == 30_000
    assert second_ms > 0
    assert second_ms <= first_ms
    assert second_ms < 30_000
    # Remaining after ~10s of a 30s budget should be ~20s (allow small skew).
    assert 15_000 <= second_ms <= 21_000
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert started_at_before["value"] is not None
        assert step.started_at is not None
        # Retry must not reset started_at (DB may strip tzinfo on round-trip).
        assert abs(
            (step.started_at.replace(tzinfo=UTC) - started_at_before["value"]).total_seconds()
        ) < 0.001
