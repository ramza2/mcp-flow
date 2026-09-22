"""PostgreSQL integration tests for McpToolRunner (docs/04 §14, docs/05 §13.6)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.agent.plan_validator import PlanValidatorService
from app.core.secret_crypto import encrypt_secret_payload
from app.core.secrets import DatabaseSecretResolver
from app.domain.enums import (
    ExecutionStatus,
    MCPAuthType,
    MCPToolStatus,
    ResourceGrantResourceType,
    RiskClass,
    SecretKind,
    SecretStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner, _PreparedCall
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError
from app.models.auth import ResourceGrant
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.secret import SecretRecordRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready
from tests.integration.test_execution_creation import _seed_validating as _seed_validating_pg


def _random_master_key() -> bytes:
    while True:
        candidate = os.urandom(32)
        if candidate.strip() == candidate:
            return candidate


class _NeverCalledMCPClient:
    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("MCP call_tool must not be invoked for this scenario")


class _StubCurrentMCPClient:
    def __init__(
        self,
        *,
        result: NormalizedToolResult | None = None,
        error: MCPClientError | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint: str, **kwargs: Any):
        snapshot = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, {"http_status": 200}, datetime.now(UTC)


def _unimplemented_resolver_factory(_session: AsyncSession):
    from app.core.secrets import UnimplementedSecretResolver

    return UnimplementedSecretResolver()


async def _seed_ready_with_risk_class(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    risk_class: str,
) -> dict[str, Any]:
    async with session_factory() as session:
        seeded = await _seed_validating_pg(session)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.risk_class = risk_class
        await session.commit()
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "READY"
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        return {**seeded, "requester_id": request.requester_id}


async def _claim_ready_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    seeded: dict[str, Any] | None = None,
    mutate_server: Callable[[Any], None] | None = None,
    worker_id: str = "pg-runner-worker",
) -> tuple[uuid.UUID, str, uuid.UUID]:
    async with session_factory() as session:
        seed = seeded if seeded is not None else await _seed_ready(session)
        created = await _create(session, seed, idempotency_key=_idem_key())
        execution_id = created.result.id

    async with session_factory() as session:
        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        assert staged == 1
        await session.commit()

    async with session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim.claimed is True
        assert claim.lease_token is not None
        await session.commit()
        lease_token = claim.lease_token

    if mutate_server is not None:
        async with session_factory() as session:
            steps = await ExecutionRepository(session).list_steps(execution_id)
            step = steps[0]
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

    return execution_id, worker_id, lease_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_runner_happy_path_secret_safe_persistence(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "sunny"}],
            structured_content=None,
            raw_size_bytes=32,
            duration_ms=4,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.worker_id is None
        assert execution.lease_token is None

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.SUCCEEDED.value
        assert step.result_inline is not None

        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.SUCCEEDED.value

        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
        assert tool_calls[0].response_meta == {"http_status": 200}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_runner_non_idempotent_timeout_persists_unknown_outcome(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_ready_with_risk_class(
        integration_session_factory, risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value
    )
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory, seeded=seeded
    )

    client = _StubCurrentMCPClient(
        error=MCPClientError(
            error_layer="TIMEOUT",
            error_code="MCP_CONNECTION_TIMEOUT",
            message="MCP server connection timed out.",
            retryable=True,
            outcome_unknown=True,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.UNKNOWN_OUTCOME.value
    assert len(client.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        # Execution has no canonical UNKNOWN_OUTCOME status — must fail closed.
        assert execution.status == ExecutionStatus.FAILED.value

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.UNKNOWN_OUTCOME.value

        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value

        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert (
            tool_calls[0].normalized_status
            == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_runner_bearer_secret_absent_from_all_rows(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    master_key = _random_master_key()
    token_value = "sk-pg-bearer-secret-value-777"

    async with integration_session_factory() as session:
        blob = encrypt_secret_payload(
            master_key, kind=SecretKind.API_KEY.value, material={"value": token_value}
        )
        record = await SecretRecordRepository(session).create(
            name=f"secret-{uuid.uuid4().hex[:8]}",
            secret_kind=SecretKind.API_KEY.value,
            ciphertext=blob.ciphertext,
            nonce=blob.nonce,
            key_version=blob.key_version,
            fingerprint=blob.fingerprint,
            status=SecretStatus.ACTIVE.value,
        )
        await session.commit()
        secret_id = record.id

    def _set_bearer(server: Any) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = secret_id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory, mutate_server=_set_bearer
    )

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(protocol_success=True, tool_error=False)
    )

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls[0]["auth_headers"] == {"Authorization": f"Bearer {token_value}"}

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        blob_text = json.dumps(
            {
                "execution_error_message": execution.error_message,
                "execution_result_summary": execution.result_summary,
                "step_error_message": step.error_message,
                "step_result_inline": step.result_inline,
                "attempt_error_message": attempts[0].error_message,
                "attempt_result_inline": attempts[0].result_inline,
                "attempt_request_snapshot": attempts[0].request_snapshot,
                "tool_call_request_meta": tool_calls[0].request_meta,
                "tool_call_response_meta": tool_calls[0].response_meta,
            },
            default=str,
        )
        assert token_value not in blob_text
        assert "Bearer" not in blob_text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_runner_after_succeeded_zero_mcp_calls(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )

    first_client = _StubCurrentMCPClient(
        result=NormalizedToolResult(protocol_success=True, tool_error=False)
    )
    first_runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=first_client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    first_outcome = await first_runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert first_outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(first_client.calls) == 1

    # Duplicate broker delivery replays the original claim identity — the
    # Execution lease is already cleared, so this must be a pure DB no-op.
    never_again = _NeverCalledMCPClient()
    second_runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=never_again,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    second_outcome = await second_runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert second_outcome.mcp_called is False
    assert second_outcome.reason in {"LEASE_MISMATCH", "STEP_ALREADY_TERMINAL"}

    async with integration_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_started_tool_call_never_reissued(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """RUNNING + STARTED Attempt + STARTED ToolCall → MCP call count 0, same ToolCall."""
    from app.domain.enums import (
        CURRENT_MCP_PROTOCOL_VERSION,
        MCPProtocolEra,
        MCPTransportType,
    )
    from app.execution.tool_step_attempt import ToolStepAttemptService

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )

    async with integration_session_factory() as session:
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
        session_factory=integration_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "TOOL_CALL_ALREADY_STARTED"
    assert outcome.tool_call_id == existing_tool_call_id

    async with integration_session_factory() as session:
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


# ---------------------------------------------------------------------------
# Final pre-send gate TOCTOU regressions (Phase A → tools/call)
# ---------------------------------------------------------------------------


def _install_presend_seam(
    monkeypatch: pytest.MonkeyPatch,
    runner: McpToolRunner,
    mutate,
) -> None:
    async def _seam(prepared: _PreparedCall) -> None:
        async with runner._session_factory() as session:
            await mutate(session, prepared)
            await session.commit()

    monkeypatch.setattr(runner, "_after_phase_a_before_final_gate", _seam)


async def _ctx(session: AsyncSession, prepared: _PreparedCall) -> dict[str, Any]:
    execution = await ExecutionRepository(session).get(prepared.execution_id)
    assert execution is not None
    tool_version = await MCPToolRepository(session).get_version(
        prepared.mcp_tool_version_id
    )
    assert tool_version is not None
    logical_tool = await MCPToolRepository(session).get(tool_version.mcp_tool_id)
    assert logical_tool is not None
    server = await MCPServerRepository(session).get(logical_tool.mcp_server_id)
    assert server is not None
    return {
        "execution": execution,
        "logical_tool": logical_tool,
        "server": server,
        "requester_id": execution.requester_id,
        "agent_version_id": execution.agent_version_id,
        "tool_id": logical_tool.id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_final_gate_happy_path(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )
    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(protocol_success=True, tool_error=False)
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is True
    assert len(client.calls) == 1
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_final_gate_resource_grant_revoke(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _ctx(session, prepared)
        grants = (
            await session.execute(
                select(ResourceGrant).where(
                    ResourceGrant.user_id == ctx["requester_id"],
                    ResourceGrant.resource_type
                    == ResourceGrantResourceType.MCP_TOOL.value,
                    ResourceGrant.resource_id == ctx["tool_id"],
                )
            )
        ).scalars().all()
        for grant in grants:
            await session.delete(grant)

    _install_presend_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        assert execution.lease_token is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_final_gate_policy_drift(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _ctx(session, prepared)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(ctx["tool_id"])
        assert policy is not None
        policy.max_result_bytes = int(policy.max_result_bytes) + 1

    _install_presend_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_final_gate_endpoint_drift(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _ctx(session, prepared)
        ctx["server"].endpoint_url = "https://attacker.example/mcp"

    _install_presend_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_final_gate_tool_and_server_inactive(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _ctx(session, prepared)
        ctx["logical_tool"].status = MCPToolStatus.INACTIVE.value

    _install_presend_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_final_gate_lease_lost_no_side_effect(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory
    )
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        execution = await ExecutionRepository(session).get(prepared.execution_id)
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        execution.worker_id = "takeover-worker"
        execution.lease_token = uuid.uuid4()

    _install_presend_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "LEASE_MISMATCH"
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.worker_id == "takeover-worker"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_final_gate_secret_then_authz_revoke(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master_key = _random_master_key()
    token_value = "sk-pg-presend-toctou-secret-999"

    async with integration_session_factory() as session:
        blob = encrypt_secret_payload(
            master_key, kind=SecretKind.API_KEY.value, material={"value": token_value}
        )
        record = await SecretRecordRepository(session).create(
            name=f"secret-{uuid.uuid4().hex[:8]}",
            secret_kind=SecretKind.API_KEY.value,
            ciphertext=blob.ciphertext,
            nonce=blob.nonce,
            key_version=blob.key_version,
            fingerprint=blob.fingerprint,
            status=SecretStatus.ACTIVE.value,
        )
        await session.commit()
        secret_id = record.id

    def _set_bearer(server: Any) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = secret_id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        integration_session_factory, mutate_server=_set_bearer
    )

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _ctx(session, prepared)
        grants = (
            await session.execute(
                select(ResourceGrant).where(
                    ResourceGrant.user_id == ctx["requester_id"],
                    ResourceGrant.resource_type
                    == ResourceGrantResourceType.MCP_TOOL.value,
                    ResourceGrant.resource_id == ctx["tool_id"],
                )
            )
        ).scalars().all()
        for grant in grants:
            await session.delete(grant)

    _install_presend_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        persisted = (
            str(execution.error_message or "")
            + str(step.resolved_input or "")
            + str(attempts[0].request_snapshot or "")
            + str(attempts[0].error_message or "")
            + str(tool_calls[0].request_meta or "")
        )
        assert token_value not in persisted
