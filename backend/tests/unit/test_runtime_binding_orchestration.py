"""Runtime Binding resolution + sequential orchestration end-to-end tests."""

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

from tests.unit.test_execution_creation import _install_no_side_effects, _seed_ready

_AV = uuid.uuid4()


class _SequencedMCPClient:
    """Return structured_content on first call; echo args on later calls."""

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


async def _seed_with_customer_schema(session: AsyncSession) -> dict[str, Any]:
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
    await session.flush()
    return {
        **seeded,
        "policy_snapshot": build_safe_tool_policy_snapshot(policy, approval),
    }


def _plan(
    *,
    tool_version_id: uuid.UUID,
    steps: list[dict[str, Any]],
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rewritten = []
    for step in steps:
        s = dict(step)
        cfg = dict(s.get("config") or {})
        cfg["tool_version_id"] = str(tool_version_id)
        s["config"] = cfg
        rewritten.append(s)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "binding handoff",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": inputs or {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


async def _materialize_queued(
    session: AsyncSession,
    *,
    plan_snapshot: dict[str, Any],
    seeded: dict[str, Any],
    input_snapshot: dict[str, Any] | None = None,
) -> uuid.UUID:
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan_snapshot,
            plan_hash=compute_plan_hash(plan_snapshot),
            input_snapshot=input_snapshot or {},
            policy_snapshot=dict(seeded["policy_snapshot"]),
            requested_at=datetime.now(UTC),
            trace_id="trace-binding",
        )
    )
    execution = await ExecutionRepository(session).get(outcome.execution.id)
    assert execution is not None
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    await session.flush()
    return execution.id


@pytest.mark.asyncio
async def test_step_output_handoff_two_tool_chain(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_with_customer_schema(session)
        tv = seeded["tool_version_id"]
        plan = _plan(
            tool_version_id=tv,
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
                        "tool_version_id": str(tv),
                        "bindings": {
                            "location": {
                                "kind": BindingKind.LITERAL.value,
                                "value": "Seoul",
                            }
                        },
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
                        "tool_version_id": str(tv),
                        "bindings": {
                            "customer_id": {
                                "kind": BindingKind.STEP_OUTPUT.value,
                                "step_id": "a",
                                "path": "/structured_content/customer_id",
                            }
                        },
                    },
                },
            ],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease_token = claim.lease_token
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.READY.value
        assert steps["b"].status == StepStatus.PENDING.value

    client = _SequencedMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 2
    assert client.calls[0]["arguments"]["location"] == "Seoul"
    assert client.calls[1]["arguments"]["customer_id"] == "C-100"

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.SUCCEEDED.value
        assert steps["b"].status == StepStatus.SUCCEEDED.value
        assert steps["b"].resolved_input == {"customer_id": "C-100"}
        assert steps["a"].result_inline is not None
        assert (
            steps["a"].result_inline["structured_content"]["customer_id"] == "C-100"
        )


@pytest.mark.asyncio
async def test_plan_input_handoff_to_tool(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_with_customer_schema(session)
        tv = seeded["tool_version_id"]
        plan = _plan(
            tool_version_id=tv,
            inputs={
                "location": {"type": "string", "required": True, "secret": False}
            },
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
                        "tool_version_id": str(tv),
                        "bindings": {
                            "location": {
                                "kind": BindingKind.PLAN_INPUT.value,
                                "path": "/location",
                            }
                        },
                    },
                }
            ],
        )
        execution_id = await _materialize_queued(
            session,
            plan_snapshot=plan,
            seeded=seeded,
            input_snapshot={"location": "Busan"},
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    client = _SequencedMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1
    assert client.calls[0]["arguments"]["location"] == "Busan"

    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert steps[0].resolved_input == {"location": "Busan"}


@pytest.mark.asyncio
async def test_plan_input_root_secret_plaintext_no_attempt_no_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_with_customer_schema(session)
        tv = seeded["tool_version_id"]
        plan = _plan(
            tool_version_id=tv,
            inputs={
                "location": {"type": "string", "required": True, "secret": False},
                "token": {"type": "string", "required": True, "secret": True},
            },
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
                        "tool_version_id": str(tv),
                        "bindings": {
                            "location": {
                                "kind": BindingKind.PLAN_INPUT.value,
                                "path": "/",
                            }
                        },
                    },
                }
            ],
        )
        # Tool schema expects string location; root binding would fail schema
        # after secret check — seed plaintext secret and ensure fail is pre-MCP.
        execution_id = await _materialize_queued(
            session,
            plan_snapshot=plan,
            seeded=seeded,
            input_snapshot={
                "location": "Seoul",
                "token": "plaintext-secret",
            },
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    client = _SequencedMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value
    assert outcome.reason == "EXECUTION_PRECONDITION_FAILED"
    assert len(client.calls) == 0

    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert steps[0].status == StepStatus.FAILED.value
        assert steps[0].resolved_input is None
        attempts = await ExecutionRepository(session).list_attempts(steps[0].id)
        assert len(attempts) == 0


@pytest.mark.asyncio
async def test_step_output_missing_path_no_mcp_for_b(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_with_customer_schema(session)
        tv = seeded["tool_version_id"]
        plan = _plan(
            tool_version_id=tv,
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
                        "tool_version_id": str(tv),
                        "bindings": {
                            "location": {
                                "kind": BindingKind.LITERAL.value,
                                "value": "Seoul",
                            }
                        },
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
                        "tool_version_id": str(tv),
                        "bindings": {
                            "customer_id": {
                                "kind": BindingKind.STEP_OUTPUT.value,
                                "step_id": "a",
                                "path": "/structured_content/does_not_exist",
                            }
                        },
                    },
                },
            ],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    client = _SequencedMCPClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value
    assert outcome.reason == "EXECUTION_PRECONDITION_FAILED"
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.SUCCEEDED.value
        assert steps["b"].status == StepStatus.FAILED.value
        attempts_b = await ExecutionRepository(session).list_attempts(steps["b"].id)
        assert len(attempts_b) == 0
