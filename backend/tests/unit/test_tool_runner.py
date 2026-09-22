"""Unit tests for McpToolRunner (MCP Tool Runner PR)."""

from __future__ import annotations

import json
import types
import uuid
from datetime import UTC, datetime

import pytest
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    ExecutionStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import (
    McpToolRunner,
    _apply_terminal_transition,
    _classify_mcp_failure,
)
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import (
    _create,
    _idem_key,
    _install_no_side_effects,
    _seed_ready,
)


class _StubCurrentMCPClient:
    """Deterministic MCP client stub — no real network I/O."""

    def __init__(
        self,
        *,
        result: NormalizedToolResult | None = None,
        error: MCPClientError | None = None,
    ):
        self._result = result
        self._error = error
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, {"http_status": 200}, datetime.now(UTC)


class _NeverCalledMCPClient:
    async def call_tool(self, *args, **kwargs):
        raise AssertionError("MCP call_tool must not be invoked for this scenario")


async def _claim_ready_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    mutate_server=None,
) -> tuple[uuid.UUID, str, uuid.UUID]:
    async with session_factory() as session:
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = outcome.result.id

        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()

        worker_id = "worker-a"
        claim_outcome = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim_outcome.claimed
        await session.commit()

        if mutate_server is not None:
            execution = await ExecutionRepository(session).get(execution_id)
            assert execution is not None
            steps = await ExecutionRepository(session).list_steps(execution_id)
            step = steps[0]
            from app.repositories.mcp_tool import MCPToolRepository

            tool_version = await MCPToolRepository(session).get_version(
                step.mcp_tool_version_id
            )
            assert tool_version is not None
            logical_tool = await MCPToolRepository(session).get(tool_version.mcp_tool_id)
            assert logical_tool is not None
            server = await MCPServerRepository(session).get(logical_tool.mcp_server_id)
            assert server is not None
            mutate_server(server)
            await session.commit()

        assert claim_outcome.lease_token is not None
        return execution_id, worker_id, claim_outcome.lease_token


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


@pytest.mark.asyncio
async def test_runner_success_path(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "ok"}],
            structured_content=None,
            raw_size_bytes=42,
            duration_ms=5,
        )
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )

    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1
    assert "arguments" in client.calls[0]

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.result_summary is not None

        steps = await ExecutionRepository(session).list_steps(execution_id)
        step = steps[0]
        assert step.status == StepStatus.SUCCEEDED.value
        assert step.result_inline is not None

        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.SUCCEEDED.value

        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
        assert tool_calls[0].response_meta == {"http_status": 200}


@pytest.mark.asyncio
async def test_runner_tool_error_marks_failed_tool_layer(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(protocol_success=True, tool_error=True)
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value

        steps = await ExecutionRepository(session).list_steps(execution_id)
        attempts = await ExecutionRepository(session).list_attempts(steps[0].id)
        assert attempts[0].error_layer == "TOOL"
        assert attempts[0].error_code == "RESULT_TOOL_ERROR"


@pytest.mark.asyncio
async def test_runner_fails_closed_without_mcp_call_for_unsupported_transport(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)

    def _make_stdio(server):
        server.transport_type = "STDIO"

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, mutate_server=_make_stdio
    )

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "MCP_TRANSPORT_UNSUPPORTED"
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "MCP_TRANSPORT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_runner_no_op_when_step_already_terminal(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(protocol_success=True, tool_error=False)
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    first = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert first.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1

    # Duplicate delivery after the Step already reached a terminal state must
    # never call MCP again.
    never_again = _NeverCalledMCPClient()
    runner2 = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=never_again,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    second = await runner2.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert second.mcp_called is False
    assert second.reason in {"LEASE_MISMATCH", "STEP_ALREADY_TERMINAL"}


def _tool_call_error(*, error_layer: str, outcome_unknown: bool) -> MCPClientError:
    return MCPClientError(
        error_layer=error_layer,
        error_code="MCP_TEST_ERROR",
        message="synthetic failure",
        retryable=True,
        outcome_unknown=outcome_unknown,
    )


@pytest.mark.parametrize(
    ("risk_class", "error_layer", "outcome_unknown", "expected"),
    [
        (RiskClass.READ_ONLY.value, "TIMEOUT", False, StepStatus.TIMED_OUT.value),
        (RiskClass.READ_ONLY.value, "TIMEOUT", True, StepStatus.TIMED_OUT.value),
        (RiskClass.IDEMPOTENT_WRITE.value, "TIMEOUT", True, StepStatus.TIMED_OUT.value),
        (RiskClass.READ_ONLY.value, "NETWORK", False, StepStatus.FAILED.value),
        (RiskClass.NON_IDEMPOTENT_WRITE.value, "TIMEOUT", True, StepStatus.UNKNOWN_OUTCOME.value),
        (RiskClass.DESTRUCTIVE.value, "NETWORK", True, StepStatus.UNKNOWN_OUTCOME.value),
        (RiskClass.UNKNOWN.value, "TIMEOUT", True, StepStatus.UNKNOWN_OUTCOME.value),
        (RiskClass.NON_IDEMPOTENT_WRITE.value, "TIMEOUT", False, StepStatus.FAILED.value),
        (RiskClass.DESTRUCTIVE.value, "AUTH", False, StepStatus.FAILED.value),
        # ToolPolicy.max_result_bytes overflow: adapter outcome_unknown=true;
        # unsafe → UNKNOWN_OUTCOME, safe → FAILED (not UNKNOWN).
        (
            RiskClass.NON_IDEMPOTENT_WRITE.value,
            "PROTOCOL",
            True,
            StepStatus.UNKNOWN_OUTCOME.value,
        ),
        (RiskClass.READ_ONLY.value, "PROTOCOL", True, StepStatus.FAILED.value),
        (RiskClass.IDEMPOTENT_WRITE.value, "PROTOCOL", True, StepStatus.FAILED.value),
    ],
)
def test_classify_mcp_failure_matrix(
    risk_class: str, error_layer: str, outcome_unknown: bool, expected: str
) -> None:
    exc = _tool_call_error(error_layer=error_layer, outcome_unknown=outcome_unknown)
    assert _classify_mcp_failure(exc, risk_class=risk_class) == expected


def test_classify_mcp_result_too_large_error_matrix() -> None:
    from app.mcp.errors import MCPResultTooLargeError

    exc = MCPResultTooLargeError(max_result_bytes=10)
    assert exc.outcome_unknown is True
    assert exc.retryable is False
    assert (
        _classify_mcp_failure(exc, risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value)
        == StepStatus.UNKNOWN_OUTCOME.value
    )
    assert (
        _classify_mcp_failure(exc, risk_class=RiskClass.DESTRUCTIVE.value)
        == StepStatus.UNKNOWN_OUTCOME.value
    )
    assert (
        _classify_mcp_failure(exc, risk_class=RiskClass.UNKNOWN.value)
        == StepStatus.UNKNOWN_OUTCOME.value
    )
    assert (
        _classify_mcp_failure(exc, risk_class=RiskClass.READ_ONLY.value)
        == StepStatus.FAILED.value
    )
    assert (
        _classify_mcp_failure(exc, risk_class=RiskClass.IDEMPOTENT_WRITE.value)
        == StepStatus.FAILED.value
    )


def _fake_row(**fields):
    return types.SimpleNamespace(**fields)


def test_apply_terminal_transition_unknown_outcome_never_reaches_execution() -> None:
    execution = _fake_row(
        status=ExecutionStatus.RUNNING.value,
        error_code=None,
        error_message=None,
        result_summary=None,
        finished_at=None,
        worker_id="w",
        lease_token=uuid.uuid4(),
        lease_expires_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC),
        lock_version=1,
    )
    step = _fake_row(
        step_key="tool-step",
        status=StepStatus.RUNNING.value,
        error_code=None,
        error_message=None,
        result_inline=None,
        finished_at=None,
        lock_version=1,
    )
    attempt = _fake_row(
        status=StepAttemptStatus.STARTED.value,
        error_layer=None,
        error_code=None,
        error_message=None,
        is_retryable=None,
        result_inline=None,
        finished_at=None,
    )
    tool_call = _fake_row(
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        response_meta=None,
        response_bytes=None,
        first_byte_at=None,
        finished_at=None,
    )
    exc = _tool_call_error(error_layer="TIMEOUT", outcome_unknown=True)

    terminal = _apply_terminal_transition(
        execution=execution,
        step=step,
        attempt=attempt,
        tool_call=tool_call,
        call_error=exc,
        result=None,
        response_meta=None,
        first_byte_at=None,
        risk_class=RiskClass.DESTRUCTIVE.value,
        output_schema=None,
        result_inline_max_bytes=256_000,
        now=datetime.now(UTC),
    )

    assert terminal == StepStatus.UNKNOWN_OUTCOME.value
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value
    assert attempt.status == StepAttemptStatus.UNKNOWN_OUTCOME.value
    assert tool_call.normalized_status == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    # Execution has no canonical UNKNOWN_OUTCOME status — it must fail closed.
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.worker_id is None
    assert execution.lease_token is None


@pytest.mark.asyncio
async def test_existing_started_tool_call_never_reissued(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step RUNNING + Attempt STARTED + ToolCall STARTED → no second tools/call."""
    from app.domain.enums import (
        CURRENT_MCP_PROTOCOL_VERSION,
        MCPProtocolEra,
        MCPTransportType,
    )
    from app.execution.tool_step_attempt import ToolStepAttemptService
    from app.repositories.mcp_tool import MCPToolRepository

    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)

    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        step = steps[0]
        started = await ToolStepAttemptService(session).start(
            execution_id=execution_id,
            step_execution_id=step.id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        tool_version = await MCPToolRepository(session).get_version(step.mcp_tool_version_id)
        assert tool_version is not None
        logical_tool = await MCPToolRepository(session).get(tool_version.mcp_tool_id)
        assert logical_tool is not None
        server = await MCPServerRepository(session).get(logical_tool.mcp_server_id)
        assert server is not None
        tool_call = await ExecutionRepository(session).create_tool_call(
            step_attempt_id=started.attempt_id,
            mcp_server_id=server.id,
            mcp_tool_version_id=tool_version.id,
            protocol_era=MCPProtocolEra.CURRENT.value,
            protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
            transport_type=MCPTransportType.STREAMABLE_HTTP.value,
            remote_request_id=str(uuid.uuid4()),
            request_meta={"method": "tools/call"},
            normalized_status=ToolCallNormalizedStatus.STARTED.value,
            started_at=datetime.now(UTC),
        )
        await session.commit()
        existing_tool_call_id = tool_call.id

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "TOOL_CALL_ALREADY_STARTED"
    assert outcome.tool_call_id == existing_tool_call_id
    assert outcome.terminal_status is None

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.RUNNING.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.STARTED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].id == existing_tool_call_id
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.STARTED.value


@pytest.mark.asyncio
async def test_finalize_rejects_expired_lease_after_remote_success(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote success must not overwrite terminal state when the lease expired."""
    from datetime import timedelta

    from app.core.errors import AppError

    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)

    class _ExpireLeaseThenSucceed:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def call_tool(self, endpoint, **kwargs):
            self.calls.append({"endpoint": endpoint, **kwargs})
            async with db_session_factory() as session:
                execution = await ExecutionRepository(session).get(execution_id)
                assert execution is not None
                execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            return (
                NormalizedToolResult(
                    protocol_success=True,
                    tool_error=False,
                    content=[{"type": "text", "text": "late"}],
                    raw_size_bytes=8,
                    duration_ms=1,
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _ExpireLeaseThenSucceed()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    with pytest.raises(AppError) as exc:
        await runner.run_claimed_execution(
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
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.STARTED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.STARTED.value


@pytest.mark.asyncio
async def test_none_auth_succeeds_without_master_key_loader(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth NONE + no SECRET_REF must not require master-key loading."""
    from app.core.errors import AppError
    from app.core.secrets import DatabaseSecretResolver

    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    loader_calls = {"count": 0}

    def _raising_loader() -> bytes | None:
        loader_calls["count"] += 1
        raise AppError(
            code="SECRET_MASTER_KEY_UNAVAILABLE",
            message="Secret master key file is unavailable.",
            status_code=503,
        )

    def _factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key_loader=_raising_loader)

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[],
            raw_size_bytes=4,
            duration_ms=1,
        )
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert loader_calls["count"] == 0


@pytest.mark.asyncio
async def test_bearer_bad_master_key_fails_pre_send_not_stranded_running(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing/invalid master key for BEARER fails closed pre-send (not RUNNING)."""
    from app.core.errors import AppError
    from app.core.secrets import DatabaseSecretResolver
    from app.domain.enums import MCPAuthType

    _install_no_side_effects(monkeypatch)

    def _set_bearer(server) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = uuid.uuid4()

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, mutate_server=_set_bearer
    )

    def _bad_loader() -> bytes | None:
        raise AppError(
            code="SECRET_MASTER_KEY_INVALID",
            message="Secret master key must be 32 bytes.",
            status_code=503,
        )

    def _factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key_loader=_bad_loader)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "SECRET_MASTER_KEY_INVALID"
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[0].error_code == "SECRET_MASTER_KEY_INVALID"
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.FAILED.value
        blob = json.dumps(
            {
                "execution": execution.error_message,
                "step": step.error_message,
                "attempt": attempts[0].error_message,
                "tool_call_meta": tool_calls[0].request_meta,
            },
            default=str,
        )
        assert "sk-" not in blob
        assert "Bearer" not in blob

