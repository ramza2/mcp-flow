"""Invocation secret echo redaction — unit coverage.

Covers recursive sanitize helper + Tool Runner persistence boundary for
BEARER / SECRET_REF / JSON-RPC error echoes without changing validation semantics.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.secrets import DatabaseSecretResolver
from app.domain.enums import (
    ExecutionStatus,
    MCPAuthType,
    ParameterProvenance,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.secret_redaction import (
    REDACTION_MARKER,
    collect_protected_plaintexts,
    redact_text,
    sanitize_for_persistence,
)
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError, MCPResultTooLargeError
from app.repositories.execution import ExecutionRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_tool_runner_security import (
    _claim_ready_execution,
    _seed_secret_record,
    _unimplemented_resolver_factory,
)

_SENTINEL = "SECRET_SENTINEL_DO_NOT_PERSIST_7f3c91a2b4e6"
_SHORT = "tok"
_LONG = "tok-longer-overlap"


def _random_master_key() -> bytes:
    while True:
        candidate = os.urandom(32)
        if candidate.strip() == candidate:
            return candidate


async def _assert_no_sentinel(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    sentinel: str = _SENTINEL,
) -> dict[str, Any]:
    async with session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        payload = {
            "execution_error_message": execution.error_message,
            "execution_result_summary": execution.result_summary,
            "step_error_message": step.error_message,
            "step_result_inline": step.result_inline,
            "step_resolved_input": step.resolved_input,
            "attempt_error_message": attempts[0].error_message,
            "attempt_result_inline": attempts[0].result_inline,
            "attempt_request_snapshot": attempts[0].request_snapshot,
            "tool_call_request_meta": tool_calls[0].request_meta,
            "tool_call_response_meta": tool_calls[0].response_meta,
        }
        blob = json.dumps(payload, default=str)
        assert sentinel not in blob
        return payload


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_collect_protected_sorts_longest_first_and_skips_empty() -> None:
    protected = collect_protected_plaintexts(
        auth_headers={"Authorization": f"Bearer {_LONG}"},
        secret_argument_values=["", _SHORT, _LONG],
        auth_material_values=[_SHORT],
    )
    assert "" not in protected
    assert protected[0] == f"Bearer {_LONG}" or len(protected[0]) >= len(_LONG)
    assert len(protected[0]) >= len(protected[-1])
    assert _LONG in protected
    assert _SHORT in protected
    assert f"Bearer {_LONG}" in protected


def test_sanitize_recursive_embedded_and_dict_keys() -> None:
    protected = collect_protected_plaintexts(secret_argument_values=[_SENTINEL])
    value = {
        "ok": "plain",
        "nested": {
            "msg": f"prefix:{_SENTINEL}:suffix",
            "list": [_SENTINEL, {"inner": _SENTINEL}],
        },
        _SENTINEL: "key-leak",
    }
    out = sanitize_for_persistence(value, protected)
    blob = json.dumps(out, default=str)
    assert _SENTINEL not in blob
    assert REDACTION_MARKER in out["nested"]["msg"]
    assert out["nested"]["list"][0] == REDACTION_MARKER
    assert REDACTION_MARKER in out
    assert out["ok"] == "plain"
    # Input not mutated.
    assert value["nested"]["msg"].endswith(f"{_SENTINEL}:suffix")


def test_redact_text_overlapping_prefers_longer() -> None:
    protected = collect_protected_plaintexts(secret_argument_values=[_SHORT, _LONG])
    assert redact_text(f"x{_LONG}y", protected) == f"x{REDACTION_MARKER}y"
    assert _SHORT not in redact_text(_LONG, protected) or redact_text(
        _LONG, protected
    ) == REDACTION_MARKER


def test_sanitize_noop_without_protected() -> None:
    original = {"a": "b"}
    assert sanitize_for_persistence(original, ()) is original


# ---------------------------------------------------------------------------
# Runner: BEARER / SECRET_REF / error / schema / no-secret
# ---------------------------------------------------------------------------


class _EchoBearerClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint: str, **kwargs: Any):
        snapshot = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        auth = (kwargs.get("auth_headers") or {}).get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth else ""
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": f"token={token}"}],
            structured_content={"nested": {"echo": token}, token: "key-leak"},
            metadata={"debug": f"prefix:{token}:suffix", "list": [token]},
            raw_size_bytes=64,
            duration_ms=2,
        )
        response_meta = {
            "http_status": 200,
            "response_headers": {
                "X-Debug-Echo": token,
                "content-type": "application/json",
            },
        }
        return result, response_meta, datetime.now(UTC)


class _EchoSecretRefClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint: str, **kwargs: Any):
        args = dict(kwargs.get("arguments") or {})
        self.calls.append({"endpoint": endpoint, "arguments": args})
        secret = str(args.get("credential") or "")
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": secret}],
            structured_content={"credential": secret, "wrapped": f"x{secret}y"},
            metadata={"echo": secret},
            raw_size_bytes=32,
            duration_ms=1,
        )
        return result, {"http_status": 200}, datetime.now(UTC)


class _JsonRpcEchoErrorClient:
    async def call_tool(self, endpoint: str, **kwargs: Any):
        auth = (kwargs.get("auth_headers") or {}).get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth else ""
        raise MCPClientError(
            error_layer="PROTOCOL",
            error_code="MCP_JSONRPC_ERROR",
            message=f"upstream rejected token={token}",
            retryable=False,
            outcome_unknown=False,
        )


class _SchemaEchoClient:
    async def call_tool(self, endpoint: str, **kwargs: Any):
        auth = (kwargs.get("auth_headers") or {}).get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth else ""
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[],
            structured_content={"status": "ok", "token": token},
            raw_size_bytes=24,
            duration_ms=1,
        )
        return result, {"http_status": 200}, datetime.now(UTC)


class _PlainSuccessClient:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "hello"}],
            structured_content={"weather": "sunny"},
            raw_size_bytes=8,
            duration_ms=1,
        )
        return result, {"http_status": 200}, datetime.now(UTC)


_CREDENTIAL_SCHEMA = {
    "type": "object",
    "properties": {"credential": {"type": "string"}},
    "required": ["credential"],
}

_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "token": {"type": "string"},
    },
    "required": ["status", "token"],
}


@pytest.mark.asyncio
async def test_bearer_echo_must_not_persist_plaintext_sentinel(
    db_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    master_key = _random_master_key()
    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=_SENTINEL
        )

    def _set_bearer(server: Any) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = secret_id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, mutate_server=_set_bearer
    )
    client = _EchoBearerClient()

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    with caplog.at_level(logging.DEBUG):
        outcome = await runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1

    payload = await _assert_no_sentinel(db_session_factory, execution_id=execution_id)
    assert REDACTION_MARKER in json.dumps(payload["step_result_inline"], default=str)
    headers = (payload["tool_call_response_meta"] or {}).get("response_headers") or {}
    assert headers.get("X-Debug-Echo") == REDACTION_MARKER
    for record in caplog.records:
        assert _SENTINEL not in record.getMessage()


@pytest.mark.asyncio
async def test_secret_ref_argument_echo_redacted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    master_key = _random_master_key()
    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=_SENTINEL
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
    client = _EchoSecretRefClient()

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
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls[0]["arguments"]["credential"] == _SENTINEL

    payload = await _assert_no_sentinel(db_session_factory, execution_id=execution_id)
    resolved = payload["step_resolved_input"] or {}
    assert resolved.get("credential", {}).get("kind") == "SECRET_REF"
    assert _SENTINEL not in json.dumps(resolved, default=str)


@pytest.mark.asyncio
async def test_jsonrpc_error_message_echo_redacted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    master_key = _random_master_key()
    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=_SENTINEL
        )

    def _set_bearer(server: Any) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = secret_id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, mutate_server=_set_bearer
    )

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_JsonRpcEchoErrorClient(),
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.FAILED.value

    payload = await _assert_no_sentinel(db_session_factory, execution_id=execution_id)
    assert payload["attempt_error_message"] is not None
    assert REDACTION_MARKER in payload["attempt_error_message"]
    assert "MCP_JSONRPC_ERROR" not in (payload["attempt_error_message"] or "")
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].error_code == "MCP_JSONRPC_ERROR"


@pytest.mark.asyncio
async def test_schema_validation_uses_raw_then_persists_redacted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    master_key = _random_master_key()
    async with db_session_factory() as session:
        secret_id = await _seed_secret_record(
            session, master_key=master_key, value=_SENTINEL
        )

    def _set_bearer(server: Any) -> None:
        server.auth_type = MCPAuthType.BEARER.value
        server.auth_secret_id = secret_id

    def _set_output_schema(tool_version: Any) -> None:
        tool_version.output_schema = _STATUS_SCHEMA

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory,
        mutate_server=_set_bearer,
        mutate_tool_version=_set_output_schema,
    )

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_SchemaEchoClient(),
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    payload = await _assert_no_sentinel(db_session_factory, execution_id=execution_id)
    inline = payload["step_result_inline"] or {}
    structured = inline.get("structured_content") or {}
    assert structured.get("status") == "ok"
    assert structured.get("token") == REDACTION_MARKER


@pytest.mark.asyncio
async def test_no_secret_path_persists_result_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_execution_creation import _install_no_side_effects

    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory
    )
    client = _PlainSuccessClient()
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
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls == 1
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.result_inline is not None
        assert step.result_inline["content"][0]["text"] == "hello"
        assert REDACTION_MARKER not in json.dumps(step.result_inline, default=str)


@pytest.mark.asyncio
async def test_mcp_result_too_large_regression_still_unknown_for_unsafe(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_execution_creation import _install_no_side_effects

    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory, risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value
    )

    class _TooLarge:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise MCPResultTooLargeError(max_result_bytes=10)

    client = _TooLarge()
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
    assert client.calls == 1
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
        assert attempts[0].error_code == "MCP_RESULT_TOO_LARGE"
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert (
            tool_calls[0].normalized_status
            == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
        )
