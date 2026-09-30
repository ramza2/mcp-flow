"""PostgreSQL integration tests for runtime Binding resolution."""

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
from app.models.mcp import MCPToolVersion
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _seed_ready


class _SequencedMCPClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        # tool_runner clears ``arguments`` / auth_headers in finally — snapshot.
        snapshot = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        if len(self.calls) == 1:
            result = NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={
                    "customer_id": "C-100",
                    "email": "a@example.com",
                },
            )
        else:
            result = NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True},
            )
        return result, {"http_status": 200}, datetime.now(UTC)


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


async def _prepare(
    session: AsyncSession,
    *,
    steps: list[dict[str, Any]],
    inputs: dict[str, Any] | None = None,
    input_snapshot: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    seeded = await _seed_ready(session)
    await session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "customer_id": {"type": "string"},
                },
                "additionalProperties": False,
            }
        )
    )
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
        "goal": "pg binding",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": inputs or {},
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
            input_snapshot=input_snapshot or {},
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
    assert claim.lease_token is not None
    return execution.id, claim.lease_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_step_output_handoff(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token = await _prepare(
            session,
            steps=[
                {
                    "id": "a",
                    "name": "a",
                    "type": AuthorableStepType.TOOL.value,
                    "required": True,
                    "depends_on": [],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "bindings": {
                            "location": {
                                "kind": BindingKind.LITERAL.value,
                                "value": "Seoul",
                            }
                        }
                    },
                },
                {
                    "id": "b",
                    "name": "b",
                    "type": AuthorableStepType.TOOL.value,
                    "required": True,
                    "depends_on": ["a"],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "bindings": {
                            "customer_id": {
                                "kind": BindingKind.STEP_OUTPUT.value,
                                "step_id": "a",
                                "path": "/structured_content/customer_id",
                            }
                        }
                    },
                },
            ],
        )

    client = _SequencedMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-worker",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 2
    assert client.calls[1]["arguments"]["customer_id"] == "C-100"

    # Duplicate delivery must not change pinned resolved_input or add MCP calls.
    second = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-worker",
        lease_token=lease_token,
    )
    assert second.mcp_called is False
    assert len(client.calls) == 2

    async with integration_session_factory() as session:
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["b"].resolved_input == {"customer_id": "C-100"}
        attempts = await ExecutionRepository(session).list_attempts(steps["b"].id)
        assert len(attempts) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_plan_input_handoff(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token = await _prepare(
            session,
            inputs={
                "location": {"type": "string", "required": True, "secret": False}
            },
            input_snapshot={"location": "Incheon"},
            steps=[
                {
                    "id": "a",
                    "name": "a",
                    "type": AuthorableStepType.TOOL.value,
                    "required": True,
                    "depends_on": [],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "bindings": {
                            "location": {
                                "kind": BindingKind.PLAN_INPUT.value,
                                "path": "/location",
                            }
                        }
                    },
                }
            ],
        )

    client = _SequencedMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-worker",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls[0]["arguments"]["location"] == "Incheon"

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert steps[0].resolved_input == {"location": "Incheon"}
