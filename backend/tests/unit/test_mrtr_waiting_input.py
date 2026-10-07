"""MRTR WAITING_INPUT foundation (docs/04 §15 / docs/05 §13.7).

Covers durable OPEN wait, requestState secrecy, timeout EXPIRED,
duplicate delivery, stale-worker fencing, Approval→MRTR, secret-echo
fail-closed, WAITING_INPUT evidence validation, and retry regression.
Does NOT cover user response / resume (PR #41).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.approval.decision import ApprovalDecisionService
from app.core.errors import AppError
from app.core.secrets import DatabaseSecretResolver
from app.domain.enums import (
    ApprovalStatus,
    ExecutionStatus,
    MCPAuthType,
    McpInputRequestStatus,
    MCPProtocolEra,
    ParameterProvenance,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedInputRequired, NormalizedToolResult
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_approval_decision_resume import _create_approver, _enter_waiting
from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_safe_transient_retry import (
    _CONNECT_ERR,
    _claim_with_policy,
    _FailThenSucceedClient,
)
from tests.unit.test_tool_runner import _claim_ready_execution, _resolver_factory
from tests.unit.test_tool_runner_security import (
    _claim_ready_execution as _claim_ready_security,
)
from tests.unit.test_tool_runner_security import (
    _random_master_key,
    _seed_secret_record,
    _unimplemented_resolver_factory,
)

_CANARY = "MRTR-CANARY-OPAQUE-STATE-xyz-9f3a"
_INPUT_REQUESTS = {
    "city": {"type": "string", "description": "City"},
    "units": {"type": "string", "enum": ["c", "f"]},
}


class _MrtrClient:
    def __init__(
        self,
        *,
        request_state: Any = None,
        input_requests: dict[str, Any] | None = None,
        mutate_before_return=None,
    ) -> None:
        self.calls: list[dict] = []
        self._request_state = (
            {"opaque": True, "token": _CANARY}
            if request_state is None
            else request_state
        )
        self._input_requests = (
            dict(_INPUT_REQUESTS) if input_requests is None else input_requests
        )
        self._mutate_before_return = mutate_before_return

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        if self._mutate_before_return is not None:
            await self._mutate_before_return()
        return (
            NormalizedInputRequired(
                input_requests=dict(self._input_requests),
                request_state=self._request_state,
                raw_size_bytes=128,
                duration_ms=7,
            ),
            {"http_status": 200, "duration_ms": 7},
            datetime.now(UTC),
        )


def _runner(
    session_factory: async_sessionmaker[AsyncSession],
    client: Any,
) -> McpToolRunner:
    return McpToolRunner(
        session_factory=session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )


def _assert_canary_absent(payload: Any) -> None:
    if payload is None:
        return
    blob = json.dumps(payload, default=str)
    assert _CANARY not in blob


# ---------------------------------------------------------------------------
# C. Runner WAITING_INPUT happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_input_required_waiting_input_happy_path(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _MrtrClient()
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )

    assert outcome.mcp_called is True
    assert len(client.calls) == 1
    assert outcome.terminal_status == StepStatus.WAITING_INPUT.value
    assert outcome.reason == "WAITING_INPUT"

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_INPUT.value
        assert execution.finished_at is None
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.lease_expires_at is None
        assert execution.heartbeat_at is None
        assert execution.error_code is None

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        started_at = step.started_at
        assert step.status == StepStatus.WAITING_INPUT.value
        assert step.finished_at is None
        assert step.attempt_count == 1
        assert step.error_code is None

        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.status == StepAttemptStatus.STARTED.value
        assert attempt.finished_at is None
        assert attempt.result_inline is None
        assert attempt.error_code is None
        assert attempt.worker_id is None
        assert attempt.lease_expires_at is None

        tool_calls = await ExecutionRepository(session).list_tool_calls(attempt.id)
        assert len(tool_calls) == 1
        tc = tool_calls[0]
        assert tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
        assert tc.finished_at is not None
        assert tc.response_meta is not None
        assert tc.response_meta.get("result_type") == "input_required"
        assert "requestState" not in tc.response_meta
        assert "request_state" not in tc.response_meta
        assert "inputRequests" not in tc.response_meta

        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(reqs) == 1
        mir = reqs[0]
        assert mir.status == McpInputRequestStatus.OPEN.value
        assert mir.round_no == 1
        assert mir.protocol_era == "CURRENT"
        assert mir.input_requests == _INPUT_REQUESTS
        assert mir.request_state == {"opaque": True, "token": _CANARY}
        assert mir.response_payload is None
        assert mir.answered_at is None
        assert mir.answered_by is None
        assert mir.step_attempt_id == attempt.id
        assert started_at is not None
        timeout = step.step_snapshot.get("timeout_seconds")
        assert mir.expires_at == started_at + timedelta(seconds=int(timeout))


# ---------------------------------------------------------------------------
# D / E. requestState secrecy + inputRequests persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_state_only_in_mcp_input_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _MrtrClient()
    with caplog.at_level("DEBUG", logger="app"):
        await _runner(db_session_factory, client).run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )

    for record in caplog.records:
        if record.name.startswith("app."):
            assert _CANARY not in record.getMessage()

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        _assert_canary_absent(execution.error_message)
        _assert_canary_absent(execution.error_code)

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        _assert_canary_absent(step.result_inline)
        _assert_canary_absent(step.error_message)

        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        _assert_canary_absent(attempt.request_snapshot)
        _assert_canary_absent(attempt.result_inline)
        _assert_canary_absent(attempt.error_message)

        tc = (await ExecutionRepository(session).list_tool_calls(attempt.id))[0]
        _assert_canary_absent(tc.request_meta)
        _assert_canary_absent(tc.response_meta)

        mir = (
            await MCPInputRequestRepository(session).list_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )[0]
        assert mir.request_state["token"] == _CANARY
        assert mir.input_requests["city"]["type"] == "string"
        assert mir.input_requests["units"]["enum"] == ["c", "f"]
        assert mir.response_payload is None


# ---------------------------------------------------------------------------
# F. expired total Step budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_required_after_step_timeout_expires_no_waiting_input(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)

    async def _age_step() -> None:
        async with db_session_factory() as session:
            step = (await ExecutionRepository(session).list_steps(execution_id))[0]
            timeout = int(step.step_snapshot["timeout_seconds"])
            # Age past total Step budget before Phase C sees the MRTR result.
            step.started_at = datetime.now(UTC) - timedelta(seconds=timeout + 5)
            await session.commit()

    client = _MrtrClient(mutate_before_return=_age_step)
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )

    assert outcome.mcp_called is True
    assert len(client.calls) == 1
    assert outcome.terminal_status == StepStatus.TIMED_OUT.value
    assert outcome.reason == "STEP_TIMEOUT"

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.TIMED_OUT.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.lease_expires_at is None

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.TIMED_OUT.value
        assert step.status != StepStatus.WAITING_INPUT.value

        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        assert attempt.status == StepAttemptStatus.TIMED_OUT.value
        assert attempt.finished_at is not None

        tc = (await ExecutionRepository(session).list_tool_calls(attempt.id))[0]
        assert tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value

        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(reqs) == 1
        assert reqs[0].status == McpInputRequestStatus.EXPIRED.value
        assert reqs[0].request_state["token"] == _CANARY
        open_req = await MCPInputRequestRepository(session).find_open_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert open_req is None


# ---------------------------------------------------------------------------
# G. duplicate runner after WAITING_INPUT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_runner_after_waiting_input_no_second_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _MrtrClient()
    runner = _runner(db_session_factory, client)
    first = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert first.reason == "WAITING_INPUT"
    assert len(client.calls) == 1

    second = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="other-worker", lease_token=uuid.uuid4()
    )
    assert second.mcp_called is False
    assert second.reason == "WAITING_INPUT"
    assert second.terminal_status == StepStatus.WAITING_INPUT.value
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 1
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(reqs) == 1
        assert reqs[0].status == McpInputRequestStatus.OPEN.value


# ---------------------------------------------------------------------------
# H. stale worker fencing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_worker_cannot_open_mrtr_wait(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)

    async def _expire_lease() -> None:
        async with db_session_factory() as session:
            execution = await ExecutionRepository(session).get(execution_id)
            assert execution is not None
            execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    client = _MrtrClient(mutate_before_return=_expire_lease)
    with pytest.raises(AppError) as exc:
        await _runner(db_session_factory, client).run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.RUNNING.value
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        assert attempt.status == StepAttemptStatus.STARTED.value
        tc = (await ExecutionRepository(session).list_tool_calls(attempt.id))[0]
        assert tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert reqs == []


# ---------------------------------------------------------------------------
# I. retry regression — READ_ONLY transient still retries; MRTR does not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_required_never_enters_safe_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _MrtrClient()
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.reason == "WAITING_INPUT"
    assert len(client.calls) == 1
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 1
        assert len(await ExecutionRepository(session).list_attempts(step.id)) == 1


@pytest.mark.asyncio
async def test_read_only_transient_retry_still_works(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #36 regression: READ_ONLY + retryable still creates Attempt #2."""
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_with_policy(
        db_session_factory, max_attempts=2
    )
    client = _FailThenSucceedClient(first_error=_CONNECT_ERR)
    outcome = await McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    ).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert client.calls == 2
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 2
        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert reqs == []


# ---------------------------------------------------------------------------
# J. Approval → MRTR regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_resume_then_input_required_waiting_input(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _requester_id = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        allow_self_approval=True,
        risk_class=RiskClass.READ_ONLY.value,
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor,
            decision="APPROVE",
        )
        await session.commit()
        resume = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="apr-mrtr",
        )
        await session.commit()
        assert resume.claimed and resume.lease_token is not None
        token = resume.lease_token

    client = _MrtrClient()
    outcome = await McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    ).run_claimed_execution(
        execution_id=execution_id, worker_id="apr-mrtr", lease_token=token
    )
    assert outcome.reason == "WAITING_INPUT"
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_INPUT.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.WAITING_INPUT.value
        approved = await ApprovalRequestRepository(session).get(approval_id)
        assert approved is not None
        assert approved.status == ApprovalStatus.APPROVED.value
        all_for_step = await ApprovalRequestRepository(session).find_approved_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(all_for_step) == 1
        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(reqs) == 1
        assert reqs[0].status == McpInputRequestStatus.OPEN.value


# ---------------------------------------------------------------------------
# K. ordinary Tool regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_success_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_tool_runner import _StubCurrentMCPClient

    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "ok"}],
            raw_size_bytes=8,
            duration_ms=1,
        )
    )
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert reqs == []


@pytest.mark.asyncio
async def test_is_error_unchanged_no_mrtr_row(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_tool_runner import _StubCurrentMCPClient

    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=True,
            content=[{"type": "text", "text": "boom"}],
            raw_size_bytes=8,
            duration_ms=1,
        )
    )
    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert (
            await MCPInputRequestRepository(session).list_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
            == []
        )


# ---------------------------------------------------------------------------
# M. Restart durability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_input_survives_fresh_session(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    await _runner(db_session_factory, _MrtrClient()).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )

    # Fresh session — no in-memory objects from the previous TX.
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_INPUT.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.WAITING_INPUT.value
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        assert attempt.status == StepAttemptStatus.STARTED.value
        tc = (await ExecutionRepository(session).list_tool_calls(attempt.id))[0]
        assert tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
        mir = (
            await MCPInputRequestRepository(session).find_open_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )
        assert mir is not None
        assert mir.request_state["token"] == _CANARY
        assert mir.step_attempt_id == attempt.id
        assert mir.round_no == 1


# ---------------------------------------------------------------------------
# Secret echo fail-closed (A / B / C)
# ---------------------------------------------------------------------------


_BEARER_TOKEN = "sk-mrtr-bearer-echo-canary-9f3a7c"
_SECRET_REF_VALUE = "mrtr-secret-ref-plaintext-echo-4b2e"


def _assert_secret_absent(blob: str, secret: str) -> None:
    assert secret not in blob
    assert f"Bearer {secret}" not in blob


async def _assert_no_mrtr_wait(
    session: AsyncSession, *, execution_id: uuid.UUID, secret: str
) -> None:
    execution = await ExecutionRepository(session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.error_code == "MCP_MRTR_SECRET_ECHO"
    assert execution.worker_id is None
    assert execution.lease_token is None
    assert execution.lease_expires_at is None
    assert execution.heartbeat_at is None
    assert execution.status != ExecutionStatus.WAITING_INPUT.value

    step = (await ExecutionRepository(session).list_steps(execution_id))[0]
    assert step.status == StepStatus.FAILED.value
    assert step.status != StepStatus.WAITING_INPUT.value
    attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
    assert attempt.status == StepAttemptStatus.FAILED.value
    assert attempt.is_retryable is False
    tc = (await ExecutionRepository(session).list_tool_calls(attempt.id))[0]
    assert tc.normalized_status == ToolCallNormalizedStatus.FAILED.value
    assert (
        await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        == []
    )
    payload = json.dumps(
        {
            "execution_error": execution.error_message,
            "execution_summary": execution.result_summary,
            "step_error": step.error_message,
            "step_result": step.result_inline,
            "attempt_error": attempt.error_message,
            "attempt_result": attempt.result_inline,
            "attempt_request": attempt.request_snapshot,
            "tc_request": tc.request_meta,
            "tc_response": tc.response_meta,
        },
        default=str,
    )
    _assert_secret_absent(payload, secret)


@pytest.mark.asyncio
async def test_bearer_echo_in_request_state_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    master_key = _random_master_key()
    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=_BEARER_TOKEN
        )

    def _set_bearer(server: Any) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = secret_id

    execution_id, worker_id, lease_token = await _claim_ready_security(
        db_session_factory, mutate_server=_set_bearer
    )
    client = _MrtrClient(
        request_state={"opaque": True, "echo": _BEARER_TOKEN},
    )

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    with caplog.at_level("DEBUG", logger="app"):
        outcome = await runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )

    assert outcome.mcp_called is True
    assert len(client.calls) == 1
    assert outcome.terminal_status == StepStatus.FAILED.value
    assert outcome.reason == "MCP_MRTR_SECRET_ECHO"
    for record in caplog.records:
        if record.name.startswith("app."):
            assert _BEARER_TOKEN not in record.getMessage()

    async with db_session_factory() as session:
        await _assert_no_mrtr_wait(
            session, execution_id=execution_id, secret=_BEARER_TOKEN
        )


@pytest.mark.asyncio
async def test_secret_ref_echo_in_input_requests_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    master_key = _random_master_key()
    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=_SECRET_REF_VALUE
        )

    credential_schema = {
        "type": "object",
        "properties": {"credential": {"type": "string"}},
        "required": ["credential"],
    }
    execution_id, worker_id, lease_token = await _claim_ready_security(
        db_session_factory,
        seeded_kwargs={
            "input_schema": credential_schema,
            "entities": [
                {
                    "name": "credential",
                    "value": str(secret_id),
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                }
            ],
        },
    )
    client = _MrtrClient(
        input_requests={
            "credential": {
                "type": "string",
                "description": f"confirm {_SECRET_REF_VALUE}",
            }
        },
    )

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    with caplog.at_level("DEBUG", logger="app"):
        outcome = await runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )

    assert outcome.mcp_called is True
    assert len(client.calls) == 1
    assert outcome.reason == "MCP_MRTR_SECRET_ECHO"
    for record in caplog.records:
        if record.name.startswith("app."):
            assert _SECRET_REF_VALUE not in record.getMessage()

    async with db_session_factory() as session:
        await _assert_no_mrtr_wait(
            session, execution_id=execution_id, secret=_SECRET_REF_VALUE
        )


@pytest.mark.asyncio
async def test_non_secret_request_state_still_persists_exactly(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    opaque = {"nonce": "not-a-resolved-secret", "round": 1, "bytes": [1, 2, 3]}
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    outcome = await _runner(
        db_session_factory, _MrtrClient(request_state=opaque)
    ).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.reason == "WAITING_INPUT"
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        mir = (
            await MCPInputRequestRepository(session).find_open_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )
        assert mir is not None
        assert mir.request_state == opaque


# ---------------------------------------------------------------------------
# WAITING_INPUT evidence validation (E–K)
# ---------------------------------------------------------------------------


async def _enter_waiting_input(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[uuid.UUID, Any]:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(session_factory)
    client = _MrtrClient()
    outcome = await _runner(session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.reason == "WAITING_INPUT"
    assert len(client.calls) == 1
    return execution_id, client


@pytest.mark.asyncio
async def test_valid_waiting_input_duplicate_delivery_noop(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, client = await _enter_waiting_input(db_session_factory, monkeypatch)
    second = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="dup", lease_token=uuid.uuid4()
    )
    assert second.mcp_called is False
    assert second.reason == "WAITING_INPUT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_waiting_input_missing_open_is_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, client = await _enter_waiting_input(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        for row in reqs:
            await session.delete(row)
        await session.commit()

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="x", lease_token=uuid.uuid4()
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_waiting_input_wrong_attempt_is_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, client = await _enter_waiting_input(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        req = (
            await MCPInputRequestRepository(session).list_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )[0]
        req.step_attempt_id = uuid.uuid4()
        await session.commit()

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="x", lease_token=uuid.uuid4()
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_waiting_input_attempt_not_started_is_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, client = await _enter_waiting_input(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        attempt.status = StepAttemptStatus.FAILED.value
        attempt.finished_at = datetime.now(UTC)
        await session.commit()

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="x", lease_token=uuid.uuid4()
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_waiting_input_toolcall_restarted_is_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, client = await _enter_waiting_input(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        tc = (await ExecutionRepository(session).list_tool_calls(attempt.id))[0]
        tc.normalized_status = ToolCallNormalizedStatus.STARTED.value
        tc.finished_at = None
        await session.commit()

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="x", lease_token=uuid.uuid4()
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_waiting_input_non_null_lease_is_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, client = await _enter_waiting_input(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.worker_id = "zombie"
        execution.lease_token = uuid.uuid4()
        execution.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        execution.heartbeat_at = datetime.now(UTC)
        await session.commit()

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="x", lease_token=uuid.uuid4()
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_waiting_input_duplicate_open_is_conflict(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, client = await _enter_waiting_input(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        existing = (
            await MCPInputRequestRepository(session).list_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )[0]
        await MCPInputRequestRepository(session).create(
            execution_id=execution_id,
            step_execution_id=step.id,
            step_attempt_id=attempt.id,
            protocol_era=MCPProtocolEra.CURRENT.value,
            input_requests={"extra": {"type": "string"}},
            request_state={"dup": True},
            round_no=1,
            status=McpInputRequestStatus.OPEN.value,
            requested_at=existing.requested_at,
            expires_at=existing.expires_at,
        )
        await session.commit()
        opens = await MCPInputRequestRepository(session).list_open_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(opens) == 2

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="x", lease_token=uuid.uuid4()
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert len(client.calls) == 1
