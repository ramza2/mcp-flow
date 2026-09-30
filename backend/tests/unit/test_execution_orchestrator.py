"""Unit tests for sequential TOOL-chain ExecutionOrchestrator (PR #44)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.errors import AppError
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.orchestrator import (
    ExecutionOrchestrator,
    validate_sequential_tool_chain,
)
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects, _seed_ready

_AV = uuid.uuid4()


class _StubCurrentMCPClient:
    def __init__(
        self,
        *,
        result: NormalizedToolResult | None = None,
        error: MCPClientError | None = None,
        fail_on_call: int | None = None,
    ):
        self._result = result
        self._error = error
        self._fail_on_call = fail_on_call
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        if self._fail_on_call is not None and len(self.calls) == self._fail_on_call:
            raise MCPClientError(
                error_layer="TRANSPORT",
                error_code="MCP_TEST_ERROR",
                message="synthetic mid-chain failure",
                retryable=False,
                outcome_unknown=False,
            )
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result, {"http_status": 200}, datetime.now(UTC)


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


def _plan(steps: list[dict[str, Any]], tool_version_id: uuid.UUID) -> dict[str, Any]:
    rewritten: list[dict[str, Any]] = []
    for step in steps:
        s = dict(step)
        cfg = dict(s.get("config") or {})
        cfg["tool_version_id"] = str(tool_version_id)
        s["config"] = cfg
        rewritten.append(s)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "sequential orchestration fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


async def _seed_executable(session: AsyncSession) -> dict[str, Any]:
    """Full authz + ToolPolicy seed reused from AgentRequest creation fixtures."""
    seeded = await _seed_ready(session)
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    return {
        **seeded,
        "policy_snapshot": build_safe_tool_policy_snapshot(policy, approval),
    }


async def _materialize_queued(
    session: AsyncSession,
    *,
    plan_snapshot: dict[str, Any],
    seeded: dict[str, Any],
) -> uuid.UUID:
    digest = compute_plan_hash(plan_snapshot)
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan_snapshot,
            plan_hash=digest,
            input_snapshot={},
            policy_snapshot=dict(seeded["policy_snapshot"]),
            requested_at=datetime.now(UTC),
            trace_id="trace-orch",
        )
    )
    execution = await ExecutionRepository(session).get(outcome.execution.id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CREATED.value
    # Queue stager currently stages AGENT_REQUEST only — unit tests stage
    # MANUAL_TOOL_TEST directly for claim/orchestrator coverage.
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    await session.flush()
    return execution.id


def test_validate_sequential_chain_accepts_linear_tools() -> None:
    tv = uuid.uuid4()
    plan = ExecutionPlanV1.model_validate(
        _plan([_tool("a"), _tool("b", depends_on=["a"]), _tool("c", depends_on=["b"])], tv)
    )
    # Lightweight stand-ins — only fields used by the validator.
    from types import SimpleNamespace

    steps = [
        SimpleNamespace(
            step_key=ps.id,
            step_type=AuthorableStepType.TOOL.value,
            parent_step_id=None,
            step_snapshot=ps.model_dump(mode="json"),
            mcp_tool_version_id=tv,
        )
        for ps in plan.steps
    ]
    chain = validate_sequential_tool_chain(plan, steps)  # type: ignore[arg-type]
    assert chain.root_step_key == "a"
    assert chain.ordered_step_keys == ("a", "b", "c")


def test_validate_sequential_chain_rejects_fan_out() -> None:
    tv = uuid.uuid4()
    plan = ExecutionPlanV1.model_validate(
        _plan(
            [
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["a"]),
            ],
            tv,
        )
    )
    from types import SimpleNamespace

    steps = [
        SimpleNamespace(
            step_key=ps.id,
            step_type=AuthorableStepType.TOOL.value,
            parent_step_id=None,
            step_snapshot=ps.model_dump(mode="json"),
            mcp_tool_version_id=tv,
        )
        for ps in plan.steps
    ]
    with pytest.raises(AppError) as exc:
        validate_sequential_tool_chain(plan, steps)  # type: ignore[arg-type]
    assert "fan-out" in exc.value.message


@pytest.mark.asyncio
async def test_claim_promotes_only_root_ready(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [_tool("tool_a"), _tool("tool_b", depends_on=["tool_a"])],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.claimed
        assert len(claim.ready_step_ids) == 1

        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["tool_a"].status == StepStatus.READY.value
        assert by_key["tool_a"].ready_at is not None
        assert by_key["tool_b"].status == StepStatus.PENDING.value
        assert by_key["tool_b"].ready_at is None
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.lease_token is not None


@pytest.mark.asyncio
async def test_claim_rejects_fan_out_graph(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["a"]),
            ],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        await session.commit()

    async with db_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ExecutionClaimService(session, lease_seconds=60).claim(
                execution_id=execution_id, worker_id="worker-a"
            )
        assert "fan-out" in exc.value.message
        await session.rollback()

        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.QUEUED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert all(s.status == StepStatus.PENDING.value for s in steps)


@pytest.mark.asyncio
async def test_sequential_two_tool_success_keeps_execution_running_until_end(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [_tool("tool_a"), _tool("tool_b", depends_on=["tool_a"])],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.claimed
        assert claim.lease_token is not None
        lease_token = claim.lease_token

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

    # Drive Step A alone first — Execution must stay RUNNING.
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        root = next(s for s in steps if s.step_key == "tool_a")
        root_id = root.id

    step_a = await runner.run_claimed_tool_step(
        execution_id=execution_id,
        step_execution_id=root_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert step_a.terminal_status == StepStatus.SUCCEEDED.value
    assert step_a.reason == "STEP_SUCCEEDED"
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.lease_token == lease_token
        steps = {s.step_key: s for s in await ExecutionRepository(session).list_steps(execution_id)}
        assert steps["tool_a"].status == StepStatus.SUCCEEDED.value
        assert steps["tool_b"].status == StepStatus.PENDING.value

    orch = ExecutionOrchestrator(
        session_factory=db_session_factory, tool_runner=runner
    )
    # Explicit progression: root SUCCEEDED → next PENDING becomes READY.
    progressed = await orch._promote_after_success(
        execution_id=execution_id,
        completed_step_id=root_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert progressed.execution_complete is False

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.lease_token == lease_token
        steps = {s.step_key: s for s in await ExecutionRepository(session).list_steps(execution_id)}
        assert steps["tool_b"].status == StepStatus.READY.value
        assert steps["tool_b"].ready_at is not None

    outcome = await orch.run(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 2

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.finished_at is not None
        assert execution.lease_token is None
        steps = {s.step_key: s for s in await ExecutionRepository(session).list_steps(execution_id)}
        assert steps["tool_a"].status == StepStatus.SUCCEEDED.value
        assert steps["tool_b"].status == StepStatus.SUCCEEDED.value
        for key in ("tool_a", "tool_b"):
            attempts = await ExecutionRepository(session).list_attempts(steps[key].id)
            assert len(attempts) == 1
            assert attempts[0].status == StepAttemptStatus.SUCCEEDED.value
            tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
            assert len(tool_calls) == 1
            assert (
                tool_calls[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
            )


@pytest.mark.asyncio
async def test_end_to_end_claim_plus_orchestrator_three_tool_chain(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["b"]),
            ],
            seeded["tool_version_id"],
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
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 3

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert {s.status for s in steps} == {StepStatus.SUCCEEDED.value}


@pytest.mark.asyncio
async def test_mid_chain_failure_does_not_run_downstream(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["b"]),
            ],
            seeded["tool_version_id"],
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

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(protocol_success=True, tool_error=False),
        fail_on_call=2,  # fail TOOL B
    )
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
    assert outcome.terminal_status == StepStatus.FAILED.value
    assert len(client.calls) == 2  # A + B only; C never called

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        steps = {s.step_key: s for s in await ExecutionRepository(session).list_steps(execution_id)}
        assert steps["a"].status == StepStatus.SUCCEEDED.value
        assert steps["b"].status == StepStatus.FAILED.value
        assert steps["c"].status == StepStatus.PENDING.value
        assert steps["c"].ready_at is None
