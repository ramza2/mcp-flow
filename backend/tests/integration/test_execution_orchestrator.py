"""PostgreSQL integration tests for sequential TOOL orchestration."""

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
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
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


class _StubCurrentMCPClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        return (
            NormalizedToolResult(protocol_success=True, tool_error=False),
            {"http_status": 200},
            datetime.now(UTC),
        )


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _tool(sid: str, *, depends_on: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {
            "tool_version_id": str(uuid.uuid4()),
            "bindings": {
                "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
            },
        },
    }


def _plan(tool_version_id: uuid.UUID, steps: list[dict[str, Any]]) -> dict[str, Any]:
    rewritten = []
    for step in steps:
        s = dict(step)
        cfg = dict(s.get("config") or {})
        cfg["tool_version_id"] = str(tool_version_id)
        s["config"] = cfg
        rewritten.append(s)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "pg sequential orchestration",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_sequential_three_tool_orchestration(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        approval = None
        if policy.approval_policy_id is not None:
            approval = await ApprovalPolicyRepository(session).get(
                policy.approval_policy_id
            )
        policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)
        plan = _plan(
            seeded["tool_version_id"],
            [
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["b"]),
            ],
        )
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
            )
        )
        execution = outcome.execution
        execution.status = ExecutionStatus.QUEUED.value
        execution.queued_at = datetime.now(UTC)
        execution.lock_version += 1
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution.id, worker_id="pg-worker"
        )
        await session.commit()
        assert claim.claimed
        assert claim.lease_token is not None
        execution_id = execution.id
        lease_token = claim.lease_token

        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["a"].status == StepStatus.READY.value
        assert by_key["b"].status == StepStatus.PENDING.value
        assert by_key["c"].status == StepStatus.PENDING.value

    client = _StubCurrentMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    result = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-worker",
        lease_token=lease_token,
    )
    assert result.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 3

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.finished_at is not None
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 3
        assert {s.status for s in steps} == {StepStatus.SUCCEEDED.value}
