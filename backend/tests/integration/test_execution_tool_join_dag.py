"""PostgreSQL integration tests for TOOL/JOIN wave DAG runtime (PR #47)."""

from __future__ import annotations

import asyncio
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
    JoinPolicy,
    RiskClass,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.dag import count_tool_slots
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


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    on_error: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": True,
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


def _join(
    sid: str,
    *,
    depends_on: list[str],
    policy: str = JoinPolicy.ALL_SUCCESS.value,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.JOIN.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"policy": policy},
    }


def _plan(
    tool_version_id: uuid.UUID,
    steps: list[dict[str, Any]],
    *,
    max_parallelism: int = 4,
) -> dict[str, Any]:
    rewritten = []
    for step in steps:
        s = dict(step)
        if s["type"] == AuthorableStepType.TOOL.value:
            cfg = dict(s.get("config") or {})
            cfg["tool_version_id"] = str(tool_version_id)
            s["config"] = cfg
        rewritten.append(s)
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = max_parallelism
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "pg tool/join dag",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


async def _materialize_claim(
    session: AsyncSession,
    *,
    step_specs: list[dict[str, Any]],
    max_parallelism: int = 4,
    worker_id: str = "pg-dag",
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    seeded = await _seed_ready(session)
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)
    plan = _plan(
        seeded["tool_version_id"], step_specs, max_parallelism=max_parallelism
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
    claim = await ExecutionClaimService(session, lease_seconds=120).claim(
        execution_id=execution.id, worker_id=worker_id
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    return execution.id, claim.lease_token, seeded


class _BarrierOverlapClient:
    def __init__(self, parties: int = 2) -> None:
        self.calls: list[dict] = []
        self.arrived = asyncio.Event()
        self._barrier = asyncio.Barrier(parties)
        self.peak = 0
        self._inflight = 0
        self._lock = asyncio.Lock()

    async def call_tool(self, endpoint, **kwargs):
        async with self._lock:
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
        self.calls.append({"tool_name": kwargs.get("tool_name")})
        if len(self.calls) >= 2:
            self.arrived.set()
        await self._barrier.wait()
        async with self._lock:
            self._inflight -= 1
        return (
            NormalizedToolResult(protocol_success=True, tool_error=False),
            {"http_status": 200},
            datetime.now(UTC),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_actual_concurrent_wave_overlap(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[_tool("a"), _tool("b")],
            max_parallelism=2,
        )

    client = _BarrierOverlapClient(parties=2)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    result = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-dag",
        lease_token=lease_token,
    )
    assert result.terminal_status == ExecutionStatus.SUCCEEDED.value
    assert len(client.calls) == 2
    assert client.peak == 2
    assert client.arrived.is_set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_max_parallelism_one_no_overlap(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[
                _tool("a"),
                _tool("b"),
                _join("j", depends_on=["a", "b"]),
            ],
            max_parallelism=1,
        )

    peak = 0
    inflight = 0
    lock = asyncio.Lock()

    class _PeakClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, endpoint, **kwargs):
            nonlocal peak, inflight
            async with lock:
                inflight += 1
                peak = max(peak, inflight)
            self.calls += 1
            await asyncio.sleep(0.05)
            async with lock:
                inflight -= 1
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _PeakClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-dag",
        lease_token=lease_token,
    )
    assert client.calls == 2
    assert peak == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_root_promotion_slots(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two scheduler sessions race READY promotion; slots never exceed max_parallelism."""
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[_tool("a"), _tool("b"), _tool("c")],
            max_parallelism=2,
        )
        # Reset roots to PENDING to race promotion (claim already promoted 2).
        steps = await ExecutionRepository(session).list_steps(execution_id)
        for s in steps:
            s.status = StepStatus.PENDING.value
            s.ready_at = None
            s.lock_version += 1
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        # Keep RUNNING lease.
        await session.commit()

    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory,
        tool_runner=object(),
    )

    async def _reserve() -> list[uuid.UUID]:
        prepared = await orch._prepare_wave(
            execution_id=execution_id,
            worker_id="pg-dag",
            lease_token=lease_token,
        )
        return list(prepared.ready_step_ids)

    results = await asyncio.gather(_reserve(), _reserve())
    ready_ids = {sid for batch in results for sid in batch}

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        slots = count_tool_slots(steps)
        assert slots <= 2
        ready = [s for s in steps if s.status == StepStatus.READY.value]
        assert len(ready) <= 2
        assert len(ready_ids) <= 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_ready_step_one_attempt(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[_tool("a")],
            max_parallelism=1,
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        step_id = steps[0].id

    entered = asyncio.Event()
    release = asyncio.Event()

    class _SlowClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, endpoint, **kwargs):
            self.calls += 1
            entered.set()
            await release.wait()
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _SlowClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )

    t1 = asyncio.create_task(
        runner.run_claimed_tool_step(
            execution_id=execution_id,
            step_execution_id=step_id,
            worker_id="pg-dag",
            lease_token=lease_token,
        )
    )
    await entered.wait()
    t2 = asyncio.create_task(
        runner.run_claimed_tool_step(
            execution_id=execution_id,
            step_execution_id=step_id,
            worker_id="pg-dag",
            lease_token=lease_token,
        )
    )
    await asyncio.sleep(0.05)
    release.set()
    outcomes = await asyncio.gather(t1, t2)
    assert client.calls == 1
    async with integration_session_factory() as session:
        attempts = await ExecutionRepository(session).list_attempts(step_id)
        assert len(attempts) == 1
        tcs = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tcs) == 1
        assert tcs[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
    assert any(o.mcp_called for o in outcomes)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_join_reconcile_race_one_terminal(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[
                _tool("a"),
                _tool("b"),
                _join("j", depends_on=["a", "b"]),
            ],
            max_parallelism=2,
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        now = datetime.now(UTC)
        for key in ("a", "b"):
            by_key[key].status = StepStatus.SUCCEEDED.value
            by_key[key].started_at = now
            by_key[key].finished_at = now
            by_key[key].lock_version += 1
        # Leave JOIN PENDING for race.
        await session.commit()

    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory,
        tool_runner=object(),
    )

    async def _settle() -> Any:
        return await orch._settle_wave(
            execution_id=execution_id,
            worker_id="pg-dag",
            lease_token=lease_token,
            wave_step_ids=(),
            outcomes=[],
            plan_order=("a", "b", "j"),
        )

    r1, r2 = await asyncio.gather(_settle(), _settle())
    assert r1.execution_complete or r2.execution_complete

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["j"].status == StepStatus.SUCCEEDED.value
        attempts = await ExecutionRepository(session).list_attempts(by_key["j"].id)
        assert attempts == []
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_terminal_duplicate_delivery_noop(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[_tool("a")],
            max_parallelism=1,
        )

    client = _BarrierOverlapClient(parties=1)
    # parties=1 barrier completes immediately
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    first = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-dag",
        lease_token=lease_token,
    )
    assert first.terminal_status == ExecutionStatus.SUCCEEDED.value
    mcp_after_first = len(client.calls)

    second = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-dag",
        lease_token=lease_token,
    )
    assert second.terminal_status == ExecutionStatus.SUCCEEDED.value
    assert second.reason in {"STEP_ALREADY_TERMINAL", "EXECUTION_SUCCEEDED"}
    assert len(client.calls) == mcp_after_first

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.status != ExecutionStatus.RUNNING.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_fan_out_join_success(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["a"]),
                _join("j", depends_on=["b", "c"]),
                _tool("d", depends_on=["j"]),
            ],
            max_parallelism=2,
        )

    client = _BarrierOverlapClient(parties=2)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    # Barrier only for first overlapping wave (b,c). a and d are solo — use
    # adaptive client instead.
    class _Adaptive:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.peak = 0
            self._inflight = 0
            self._lock = asyncio.Lock()
            self._bc_barrier: asyncio.Barrier | None = None

        async def call_tool(self, endpoint, **kwargs):
            async with self._lock:
                self._inflight += 1
                self.peak = max(self.peak, self._inflight)
                self.calls.append(str(len(self.calls)))
                n = len(self.calls)
            # Calls 2 and 3 are b/c after a — overlap them.
            if n in {2, 3}:
                if self._bc_barrier is None:
                    self._bc_barrier = asyncio.Barrier(2)
                await self._bc_barrier.wait()
            async with self._lock:
                self._inflight -= 1
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _Adaptive()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    result = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-dag",
        lease_token=lease_token,
    )
    assert result.terminal_status == ExecutionStatus.SUCCEEDED.value
    assert len(client.calls) == 4
    assert client.peak >= 2

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["j"].status == StepStatus.SUCCEEDED.value
        assert await ExecutionRepository(session).list_attempts(by_key["j"].id) == []
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.lease_token is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_fatal_sibling_wave(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded_pack = await _seed_ready(session)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(
            seeded_pack["tool_id"]
        )
        assert policy is not None
        policy.risk_class = RiskClass.NON_IDEMPOTENT_WRITE.value
        approval = None
        if policy.approval_policy_id is not None:
            approval = await ApprovalPolicyRepository(session).get(
                policy.approval_policy_id
            )
        policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)
        plan = _plan(
            seeded_pack["tool_version_id"],
            [_tool("a"), _tool("b"), _tool("d", depends_on=["a"])],
            max_parallelism=2,
        )
        outcome = await ExecutionPlanMaterializer(session).materialize(
            ExecutionMaterializeParams(
                source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
                trigger_type="TEST",
                requester_id=seeded_pack["requester_id"],
                agent_version_id=seeded_pack["agent_version_id"],
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
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution.id, worker_id="pg-dag"
        )
        await session.commit()
        assert claim.lease_token is not None
        execution_id = execution.id
        lease_token = claim.lease_token

    barrier = asyncio.Barrier(2)
    release = asyncio.Event()

    class _FatalSibling:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, endpoint, **kwargs):
            self.calls += 1
            idx = self.calls
            await barrier.wait()
            await release.wait()
            if idx == 1:
                raise MCPClientError(
                    error_layer="TIMEOUT",
                    error_code="MCP_UNKNOWN",
                    message="ambiguous",
                    retryable=False,
                    outcome_unknown=True,
                )
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _FatalSibling()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    task = asyncio.create_task(
        runner.run_claimed_execution(
            execution_id=execution_id,
            worker_id="pg-dag",
            lease_token=lease_token,
        )
    )
    for _ in range(200):
        if client.calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert client.calls == 2
    release.set()
    result = await task
    assert result.reason in {"EXECUTION_FAILED", "UNKNOWN_OUTCOME"} or (
        result.terminal_status
        in {ExecutionStatus.FAILED.value, StepStatus.UNKNOWN_OUTCOME.value}
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert StepStatus.UNKNOWN_OUTCOME.value in {
            by_key["a"].status,
            by_key["b"].status,
        }
        assert StepStatus.SUCCEEDED.value in {by_key["a"].status, by_key["b"].status}
        assert by_key["d"].status == StepStatus.SKIPPED.value
        assert client.calls == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_post_claim_step_snapshot_tamper_fail_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After claim, mutate step_snapshot only; orchestrator fail-closed, MCP 0."""
    async with integration_session_factory() as session:
        execution_id, lease_token, _ = await _materialize_claim(
            session,
            step_specs=[
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["a"]),
                _join(
                    "j",
                    depends_on=["b", "c"],
                    policy=JoinPolicy.ALL_SUCCESS.value,
                ),
                _tool("d", depends_on=["j"]),
            ],
            max_parallelism=2,
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        # Tamper JOIN policy in persisted snapshot only.
        snap = dict(by_key["j"].step_snapshot)
        cfg = dict(snap["config"])
        cfg["policy"] = JoinPolicy.ALL_COMPLETE.value
        snap["config"] = cfg
        by_key["j"].step_snapshot = snap
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        plan_hash = execution.plan_hash
        plan_snapshot = dict(execution.plan_snapshot)
        await session.commit()

    client = _BarrierOverlapClient(parties=2)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    result = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-dag",
        lease_token=lease_token,
    )
    assert len(client.calls) == 0
    assert result.terminal_status == ExecutionStatus.FAILED.value
    assert result.reason == "EXECUTION_FAILED"

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "RESOURCE_CONFLICT"
        assert execution.lease_token is None
        assert execution.worker_id is None
        assert execution.plan_hash == plan_hash
        assert execution.plan_snapshot == plan_snapshot
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["a"].status == StepStatus.CANCELLED.value
        assert by_key["a"].attempt_count == 0
        for key in ("b", "c", "j", "d"):
            assert by_key[key].status == StepStatus.SKIPPED.value
            attempts = await ExecutionRepository(session).list_attempts(by_key[key].id)
            assert attempts == []
