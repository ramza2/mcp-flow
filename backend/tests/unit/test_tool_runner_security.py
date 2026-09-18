"""Security-focused unit tests for McpToolRunner (docs/04 §14 / docs/05 §5.3, §13.6).

Covers: fail-closed requires_approval, BEARER auth secret-safety, SECRET_REF
argument materialization, output schema validation leak-safety, the
NON_IDEMPOTENT_WRITE + outcome_unknown -> UNKNOWN_OUTCOME matrix, and
lease-fencing no-op behavior.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from app.agent.plan_validator import PlanValidatorService
from app.core.errors import AppError
from app.core.secret_crypto import encrypt_secret_payload
from app.core.secrets import DatabaseSecretResolver, UnimplementedSecretResolver
from app.domain.enums import (
    ExecutionStatus,
    MCPAuthType,
    ParameterProvenance,
    RiskClass,
    SecretKind,
    SecretStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.secret import SecretRecordRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import (
    _create,
    _idem_key,
    _install_no_side_effects,
    _seed_ready,
)
from tests.unit.test_plan_validator import _seed_validating
from tests.unit.test_tool_runner import _NeverCalledMCPClient, _StubCurrentMCPClient


def _random_master_key() -> bytes:
    while True:
        candidate = os.urandom(32)
        if candidate.strip() == candidate:
            return candidate


async def _seed_secret_record(
    session: AsyncSession,
    *,
    master_key: bytes,
    value: str,
    status: str = SecretStatus.ACTIVE.value,
) -> uuid.UUID:
    blob = encrypt_secret_payload(
        master_key, kind=SecretKind.API_KEY.value, material={"value": value}
    )
    record = await SecretRecordRepository(session).create(
        name=f"secret-{uuid.uuid4().hex[:8]}",
        secret_kind=SecretKind.API_KEY.value,
        ciphertext=blob.ciphertext,
        nonce=blob.nonce,
        key_version=blob.key_version,
        fingerprint=blob.fingerprint,
        status=status,
    )
    await session.commit()
    return record.id


async def _claim_ready_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    seeded_kwargs: dict[str, Any] | None = None,
    risk_class: str | None = None,
    mutate_server: Callable[[Any], None] | None = None,
    mutate_tool_version: Callable[[Any], None] | None = None,
    worker_id: str = "worker-sec",
) -> tuple[uuid.UUID, str, uuid.UUID]:
    async with session_factory() as session:
        kwargs = dict(seeded_kwargs or {})
        if risk_class is not None:
            seeded = await _seed_validating(session, **kwargs)
            policy = await MCPToolPolicyRepository(session).get_by_tool_id(
                seeded["tool_id"]
            )
            assert policy is not None
            policy.risk_class = risk_class
            await session.commit()
            outcome = await PlanValidatorService(session).validate(
                agent_request_id=seeded["request_id"]
            )
            assert outcome.decision == "READY"
            request = await AgentRequestRepository(session).get(seeded["request_id"])
            assert request is not None
            seeded = {**seeded, "requester_id": request.requester_id}
        else:
            seeded = await _seed_ready(session, **kwargs)

        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id

        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()

        claim_outcome = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim_outcome.claimed
        await session.commit()

        if mutate_server is not None or mutate_tool_version is not None:
            steps = await ExecutionRepository(session).list_steps(execution_id)
            step = steps[0]
            tool_version = await MCPToolRepository(session).get_version(
                step.mcp_tool_version_id
            )
            assert tool_version is not None
            if mutate_tool_version is not None:
                mutate_tool_version(tool_version)
            if mutate_server is not None:
                logical_tool = await MCPToolRepository(session).get(
                    tool_version.mcp_tool_id
                )
                assert logical_tool is not None
                server = await MCPServerRepository(session).get(
                    logical_tool.mcp_server_id
                )
                assert server is not None
                mutate_server(server)
            await session.commit()

        assert claim_outcome.lease_token is not None
        return execution_id, worker_id, claim_outcome.lease_token


def _unimplemented_resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


class _RecordingClient:
    """Records arguments/auth_headers passed to a fake successful call_tool."""

    def __init__(self, *, structured_content: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._structured_content = structured_content

    async def call_tool(self, endpoint: str, **kwargs: Any):
        # Copy mutable dict args — the runner clears them in-place after the
        # call returns (secret-safety), which would otherwise blank our record.
        snapshot = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[],
            structured_content=self._structured_content,
            raw_size_bytes=16,
            duration_ms=3,
        )
        return result, {"http_status": 200}, datetime.now(UTC)


# ---------------------------------------------------------------------------
# requires_approval fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_approval_fail_closed_never_calls_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Runner Approval Gate",
        )
        await session.commit()
        approval_id = approval.id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory,
        seeded_kwargs={
            "policy_requires_approval": True,
            "approval_policy_id": approval_id,
        },
    )

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    with pytest.raises(AppError) as exc:
        await runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )
    assert exc.value.status_code == 409
    assert "requires_approval" in exc.value.message.lower()

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.READY.value
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []


# ---------------------------------------------------------------------------
# BEARER auth: secret is passed to MCP but never persisted/logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_auth_header_sent_but_never_persisted_or_logged(
    db_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Not using _install_no_side_effects: this test exercises a *real*
    # DatabaseSecretResolver (ResolvedSecret construction), only the MCP
    # transport itself is faked via _RecordingClient.
    master_key = _random_master_key()
    token_value = "sk-bearer-super-secret-999"

    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=token_value
        )

    def _set_bearer(server: Any) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = secret_id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, mutate_server=_set_bearer
    )

    client = _RecordingClient()

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    with caplog.at_level("DEBUG"):
        outcome = await runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )

    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1
    assert client.calls[0]["auth_headers"] == {"Authorization": f"Bearer {token_value}"}

    for record in caplog.records:
        assert token_value not in record.getMessage()

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        blob = json.dumps(
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
        assert token_value not in blob
        assert "Bearer" not in blob


# ---------------------------------------------------------------------------
# SECRET_REF argument materialization
# ---------------------------------------------------------------------------


_CREDENTIAL_SCHEMA = {
    "type": "object",
    "properties": {"credential": {"type": "string"}},
    "required": ["credential"],
}


@pytest.mark.asyncio
async def test_secret_ref_argument_reference_only_after_success(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Not using _install_no_side_effects: this test exercises a *real*
    # DatabaseSecretResolver (ResolvedSecret construction), only the MCP
    # transport itself is faked via _RecordingClient.
    master_key = _random_master_key()
    secret_value = "sk-argument-material-secret"

    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=secret_value
        )

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory,
        seeded_kwargs={
            "input_schema": _CREDENTIAL_SCHEMA,
            "entities": [
                {
                    "name": "credential",
                    "value": str(secret_id),
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                }
            ],
        },
    )

    client = _RecordingClient()

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

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
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls[0]["arguments"] == {"credential": secret_value}

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.resolved_input == {
            "credential": {"kind": "SECRET_REF", "secret_id": str(secret_id)}
        }
        blob = json.dumps(
            {"resolved_input": step.resolved_input, "result_inline": step.result_inline},
            default=str,
        )
        assert secret_value not in blob


@pytest.mark.asyncio
async def test_missing_secret_for_secret_ref_fails_closed_no_remote_call(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    missing_secret_id = uuid.uuid4()

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory,
        seeded_kwargs={
            "input_schema": _CREDENTIAL_SCHEMA,
            "entities": [
                {
                    "name": "credential",
                    "value": str(missing_secret_id),
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                }
            ],
        },
    )

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
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
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.FAILED.value


# ---------------------------------------------------------------------------
# Result validation: schema invalid vs valid, no leak of raw structured content
# ---------------------------------------------------------------------------

_TEMP_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"temp_c": {"type": "number"}},
    "required": ["temp_c"],
}


@pytest.mark.asyncio
async def test_schema_invalid_result_fails_without_leaking_structured_content(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)

    def _set_output_schema(tool_version: Any) -> None:
        tool_version.output_schema = _TEMP_OUTPUT_SCHEMA

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, mutate_tool_version=_set_output_schema
    )

    leaked_marker = "SHOULD-NEVER-LEAK-INTO-ERROR-MESSAGE"
    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            structured_content={"temp_c": "not-a-number", "secret": leaked_marker},
        )
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.error_code == "RESULT_SCHEMA_INVALID"
        assert leaked_marker not in (execution.error_message or "")
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert leaked_marker not in (step.error_message or "")
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert leaked_marker not in (attempts[0].error_message or "")
        assert attempts[0].result_inline is None


@pytest.mark.asyncio
async def test_schema_valid_result_succeeds(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)

    def _set_output_schema(tool_version: Any) -> None:
        tool_version.output_schema = _TEMP_OUTPUT_SCHEMA

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, mutate_tool_version=_set_output_schema
    )

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            structured_content={"temp_c": 21},
        )
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.result_inline is not None
        assert step.result_inline["structured_content"] == {"temp_c": 21}


# ---------------------------------------------------------------------------
# NON_IDEMPOTENT_WRITE + outcome_unknown timeout -> UNKNOWN_OUTCOME
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_idempotent_write_timeout_unknown_outcome_calls_once(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value
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
        session_factory=db_session_factory,
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

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        # Execution has no canonical UNKNOWN_OUTCOME status — must fail closed.
        assert execution.status == ExecutionStatus.FAILED.value

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.UNKNOWN_OUTCOME.value

        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value

        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value


# ---------------------------------------------------------------------------
# Wrong lease -> never calls MCP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_lease_token_never_calls_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, _lease_token = await _claim_ready_execution(
        db_session_factory
    )

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=uuid.uuid4()
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "LEASE_MISMATCH"
