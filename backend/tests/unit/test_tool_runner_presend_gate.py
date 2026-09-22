"""Final pre-send gate TOCTOU regressions (Phase A → tools/call).

Deterministic seam: monkeypatch ``_after_phase_a_before_final_gate`` to mutate
mutable authorization/policy after TX1 commit and secret materialize, before
the short final-gate transaction. Never relies on real concurrency timing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.secret_crypto import encrypt_secret_payload
from app.core.secrets import DatabaseSecretResolver, UnimplementedSecretResolver
from app.domain.enums import (
    AgentToolGrantEffect,
    ExecutionStatus,
    MCPAuthType,
    MCPServerStatus,
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
from app.execution.recovery import ExecutionRecoveryService, RecoveryDecision
from app.execution.tool_runner import McpToolRunner, _PreparedCall
from app.mcp.contracts import NormalizedToolResult
from app.models.auth import ResourceGrant, RolePermission, UserRole
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.secret import SecretRecordRepository

from tests.unit.test_execution_creation import (
    _create,
    _idem_key,
    _install_no_side_effects,
    _seed_ready,
)
from tests.unit.test_tool_runner import (
    _NeverCalledMCPClient,
    _StubCurrentMCPClient,
    _claim_ready_execution,
)

Mutator = Callable[[AsyncSession, _PreparedCall], Awaitable[None]]


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _success_client() -> _StubCurrentMCPClient:
    return _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "ok"}],
            structured_content=None,
            raw_size_bytes=16,
            duration_ms=1,
        )
    )


def _install_seam(
    monkeypatch: pytest.MonkeyPatch,
    runner: McpToolRunner,
    mutate: Mutator,
) -> None:
    async def _seam(prepared: _PreparedCall) -> None:
        async with runner._session_factory() as session:
            await mutate(session, prepared)
            await session.commit()

    monkeypatch.setattr(runner, "_after_phase_a_before_final_gate", _seam)


async def _load_execution_context(
    session: AsyncSession, prepared: _PreparedCall
) -> dict[str, Any]:
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
        "tool_version": tool_version,
        "logical_tool": logical_tool,
        "server": server,
        "requester_id": execution.requester_id,
        "agent_version_id": execution.agent_version_id,
        "tool_id": logical_tool.id,
    }


async def _delete_mcp_tool_resource_grants(
    session: AsyncSession, *, user_id: uuid.UUID, tool_id: uuid.UUID
) -> None:
    grants = (
        await session.execute(
            select(ResourceGrant).where(
                ResourceGrant.user_id == user_id,
                ResourceGrant.resource_type
                == ResourceGrantResourceType.MCP_TOOL.value,
                ResourceGrant.resource_id == tool_id,
            )
        )
    ).scalars().all()
    for grant in grants:
        await session.delete(grant)


async def _clear_user_execute_permissions(
    session: AsyncSession, *, user_id: uuid.UUID
) -> None:
    role_ids = (
        await session.execute(select(UserRole.role_id).where(UserRole.user_id == user_id))
    ).scalars().all()
    for role_id in role_ids:
        perms = (
            await session.execute(
                select(RolePermission).where(RolePermission.role_id == role_id)
            )
        ).scalars().all()
        for perm in perms:
            await session.delete(perm)


async def _assert_failed_no_mcp(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    client: _StubCurrentMCPClient | _NeverCalledMCPClient,
    outcome: Any,
) -> None:
    assert outcome.mcp_called is False
    if isinstance(client, _StubCurrentMCPClient):
        assert len(client.calls) == 0
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.lease_expires_at is None
        assert execution.heartbeat_at is None

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.FAILED.value


@pytest.mark.asyncio
async def test_final_gate_happy_path_mcp_exactly_one(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _success_client()
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


@pytest.mark.asyncio
async def test_final_gate_rejects_resource_grant_revoke(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        await _delete_mcp_tool_resource_grants(
            session, user_id=ctx["requester_id"], tool_id=ctx["tool_id"]
        )

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_permission_revoke(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        await _clear_user_execute_permissions(session, user_id=ctx["requester_id"])

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_agent_tool_grant_revoke(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        await AgentToolGrantRepository(session).replace_all(ctx["agent_version_id"], [])

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_policy_timeout_drift(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(ctx["tool_id"])
        assert policy is not None
        policy.timeout_ms = int(policy.timeout_ms) + 1000

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_tool_inactive(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        ctx["logical_tool"].status = MCPToolStatus.INACTIVE.value

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_current_version_drift(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        ctx["logical_tool"].current_version_id = uuid.uuid4()

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_server_inactive(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        ctx["server"].status = MCPServerStatus.INACTIVE.value

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_endpoint_auth_drift(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        ctx["server"].endpoint_url = "https://evil.example/mcp"

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_lease_expiry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        execution = await ExecutionRepository(session).get(prepared.execution_id)
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "LEASE_MISMATCH"
    # Lost ownership must not terminalize (could race with takeover).
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value


@pytest.mark.asyncio
async def test_final_gate_rejects_lease_ownership_change(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        execution = await ExecutionRepository(session).get(prepared.execution_id)
        assert execution is not None
        execution.worker_id = "other-worker"
        execution.lease_token = uuid.uuid4()

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "LEASE_MISMATCH"
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.worker_id == "other-worker"


@pytest.mark.asyncio
async def test_final_gate_applies_after_takeover_with_grant_revoke(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="old-worker"
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with db_session_factory() as session:
        outcome_rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id, worker_id="new-worker"
        )
        await session.commit()
        assert outcome_rec.decision == RecoveryDecision.TAKEOVER_READY
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.worker_id == "new-worker"
        assert execution.lease_token is not None
        new_token = execution.lease_token

    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        await _delete_mcp_tool_resource_grants(
            session, user_id=ctx["requester_id"], tool_id=ctx["tool_id"]
        )

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="new-worker", lease_token=new_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_valid_takeover_still_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_ready(session)
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="old-worker"
        )
        assert claim.claimed
        await session.commit()
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with db_session_factory() as session:
        outcome_rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id, worker_id="new-worker"
        )
        await session.commit()
        assert outcome_rec.decision == RecoveryDecision.TAKEOVER_READY
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        new_token = execution.lease_token
        assert new_token is not None

    client = _success_client()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="new-worker", lease_token=new_token
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_final_gate_secret_materialize_then_authz_revoke(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real DatabaseSecretResolver (like other BEARER tests); only MCP is stubbed.
    master_key = os.urandom(32)
    while master_key.strip() != master_key:
        master_key = os.urandom(32)
    token_value = "sk-presend-gate-secret-plaintext-xyz"

    async with db_session_factory() as session:
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
        db_session_factory, mutate_server=_set_bearer
    )

    client = _NeverCalledMCPClient()

    def _factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        await _delete_mcp_tool_resource_grants(
            session, user_id=ctx["requester_id"], tool_id=ctx["tool_id"]
        )

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        persisted = (
            str(execution.error_message or "")
            + str(execution.result_summary or "")
            + str(execution.policy_snapshot or "")
            + str(step.resolved_input or "")
            + str(step.error_message or "")
            + str(attempts[0].request_snapshot or "")
            + str(attempts[0].error_message or "")
            + str(tool_calls[0].request_meta or "")
            + str(tool_calls[0].response_meta or "")
        )
        assert token_value not in persisted


@pytest.mark.asyncio
async def test_final_gate_rejects_agent_grant_deny_effect(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        await AgentToolGrantRepository(session).replace_all(
            ctx["agent_version_id"],
            [
                {
                    "mcp_tool_id": ctx["tool_id"],
                    "effect": AgentToolGrantEffect.DENY.value,
                }
            ],
        )

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_rejects_risk_class_policy_drift(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        ctx = await _load_execution_context(session, prepared)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(ctx["tool_id"])
        assert policy is not None
        policy.risk_class = RiskClass.DESTRUCTIVE.value

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_lineage_corrupt_step_status_fail_closed_terminal(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        step = await ExecutionRepository(session).lock_step(prepared.step_id)
        assert step is not None
        step.status = StepStatus.READY.value

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value
    assert outcome.reason == "RESOURCE_CONFLICT"
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_lineage_corrupt_tool_call_id_fail_closed_terminal(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        executions = ExecutionRepository(session)
        tool_call = await executions.get_tool_call_with_lock(prepared.tool_call_id)
        assert tool_call is not None
        tool_call.mcp_server_id = uuid.uuid4()

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value
    await _assert_failed_no_mcp(
        db_session_factory, execution_id=execution_id, client=client, outcome=outcome
    )


@pytest.mark.asyncio
async def test_final_gate_lineage_corrupt_attempt_status_no_overwrite_when_terminal(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        attempt = await ExecutionRepository(session).get_attempt_with_lock(
            prepared.attempt_id
        )
        assert attempt is not None
        attempt.status = StepAttemptStatus.FAILED.value

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.lease_expires_at is None
        assert execution.heartbeat_at is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        # Pre-existing terminal Attempt status is preserved (not re-written).
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.FAILED.value


@pytest.mark.asyncio
async def test_final_gate_already_terminal_evidence_no_overwrite(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        executions = ExecutionRepository(session)
        step = await executions.lock_step(prepared.step_id)
        attempt = await executions.get_attempt_with_lock(prepared.attempt_id)
        tool_call = await executions.get_tool_call_with_lock(prepared.tool_call_id)
        assert step is not None and attempt is not None and tool_call is not None
        step.status = StepStatus.SUCCEEDED.value
        attempt.status = StepAttemptStatus.SUCCEEDED.value
        tool_call.normalized_status = ToolCallNormalizedStatus.SUCCEEDED.value

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert outcome.terminal_status == StepStatus.FAILED.value
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.lease_expires_at is None
        assert execution.heartbeat_at is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        # Terminal Step/Attempt/ToolCall values preserved — no overwrite.
        assert step.status == StepStatus.SUCCEEDED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.SUCCEEDED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_final_gate_missing_tool_call_fail_closed_clears_running_strand(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def mutate(session: AsyncSession, prepared: _PreparedCall) -> None:
        from app.models.execution import ToolCall

        tool_call = await session.get(ToolCall, prepared.tool_call_id)
        assert tool_call is not None
        await session.delete(tool_call)

    _install_seam(monkeypatch, runner, mutate)
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert (
            await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        ) == []
