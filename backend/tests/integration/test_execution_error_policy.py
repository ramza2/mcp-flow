"""PostgreSQL integration tests for ErrorPolicy + ALL_REQUIRED (PR #46)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.orchestrator import ExecutionOrchestrator
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _seed_ready


class _Client:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self._fail_on = fail_on or set()

    async def call_tool(self, endpoint, **kwargs):
        snapshot = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        if len(self.calls) in self._fail_on:
            raise MCPClientError(
                error_layer="TRANSPORT",
                error_code="MCP_TEST_ERROR",
                message="known failure",
                retryable=False,
                outcome_unknown=False,
            )
        return (
            NormalizedToolResult(protocol_success=True, tool_error=False),
            {"http_status": 200},
            datetime.now(UTC),
        )


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    required: bool = True,
    on_error: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": required,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": on_error,
        "config": {
            "tool_version_id": str(uuid.uuid4()),
            "bindings": {
                "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
            },
        },
    }


async def _prepare(
    session: AsyncSession,
    *,
    steps: list[dict[str, Any]],
) -> tuple[uuid.UUID, uuid.UUID]:
    seeded = await _seed_ready(session)
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)
    tv = seeded["tool_version_id"]
    rewritten = []
    for step in steps:
        s = dict(step)
        cfg = dict(s.get("config") or {})
        cfg["tool_version_id"] = str(tv)
        s["config"] = cfg
        rewritten.append(s)
    plan = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "pg error policy",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan,
            plan_hash=compute_plan_hash(plan),
            input_snapshot={},
            policy_snapshot=policy_snapshot,
            requested_at=datetime.now(UTC),
            trace_id="trace-pg-errpol",
        )
    )
    execution = await ExecutionRepository(session).get(outcome.execution.id)
    assert execution is not None
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    await session.flush()
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=execution.id, worker_id="worker-a"
    )
    assert claim.lease_token is not None
    return execution.id, claim.lease_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_mark_partial_partially_succeeded(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token = await _prepare(
            session,
            steps=[
                _tool("a", on_error="MARK_PARTIAL"),
                _tool("b", depends_on=["a"]),
            ],
        )
        await session.commit()

    client = _Client(fail_on={1})
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="worker-a", lease_token=lease_token
    )
    assert len(client.calls) == 2

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_continue_optional_succeeded(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token = await _prepare(
            session,
            steps=[
                _tool("a", required=False, on_error="CONTINUE"),
                _tool("b", depends_on=["a"], required=True),
            ],
        )
        await session.commit()

    client = _Client(fail_on={1})
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="worker-a", lease_token=lease_token
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_fail_execution_skips_b(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token = await _prepare(
            session,
            steps=[
                _tool("a", on_error="FAIL_EXECUTION"),
                _tool("b", depends_on=["a"]),
            ],
        )
        await session.commit()

    client = _Client(fail_on={1})
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="worker-a", lease_token=lease_token
    )
    assert len(client.calls) == 1
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["b"].status == StepStatus.SKIPPED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_progress_after_continue_promotes_once(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token = await _prepare(
            session,
            steps=[
                _tool("a", on_error="CONTINUE"),
                _tool("b", depends_on=["a"]),
            ],
        )
        await session.commit()
        steps = await ExecutionRepository(session).list_steps(execution_id)
        a_id = next(s.id for s in steps if s.step_key == "a")

    client = _Client(fail_on={1})
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_tool_step(
        execution_id=execution_id,
        step_execution_id=a_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory, tool_runner=runner
    )
    first = await orch._progress_after_terminal_step(
        execution_id=execution_id,
        completed_step_id=a_id,
        worker_id="worker-a",
        lease_token=lease_token,
        allow_continuable_failure=True,
    )
    second = await orch._progress_after_terminal_step(
        execution_id=execution_id,
        completed_step_id=a_id,
        worker_id="worker-a",
        lease_token=lease_token,
        allow_continuable_failure=True,
    )
    assert first.promoted is True
    assert second.promoted is False
    assert second.reason == "ALREADY_READY"
    assert len(client.calls) == 1
