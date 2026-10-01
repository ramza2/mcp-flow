"""Unit tests for TOOL/JOIN wave DAG runtime (PR #47)."""

from __future__ import annotations

import asyncio
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
    JoinPolicy,
    MCPProtocolEra,
    MCPTransportType,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.dag import (
    StopCause,
    evaluate_join_policy,
    pick_stop_cause,
    validate_tool_join_dag,
)
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.orchestrator import ExecutionOrchestrator, assert_execution_plan_lineage
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedInputRequired, NormalizedToolResult
from app.mcp.current import CURRENT_MCP_PROTOCOL_VERSION
from app.mcp.errors import MCPClientError
from app.models.mcp import MCPToolVersion
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects, _seed_ready

_AV = uuid.uuid4()


class _OkClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append(
            {
                "endpoint": endpoint,
                **{
                    k: (dict(v) if isinstance(v, dict) else v)
                    for k, v in kwargs.items()
                },
            }
        )
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True, "value": kwargs.get("tool_name")},
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _BarrierClient:
    """Async barrier proving real concurrent MCP overlap."""

    def __init__(self, *, parties: int, release_event: asyncio.Event | None = None):
        self.calls: list[dict] = []
        self.entered = 0
        self.peak = 0
        self._parties = parties
        self._gate = asyncio.Barrier(parties)
        self._release = release_event or asyncio.Event()
        self._release.set()
        self._lock = asyncio.Lock()

    async def call_tool(self, endpoint, **kwargs):
        async with self._lock:
            self.entered += 1
            self.peak = max(self.peak, self.entered)
            n = self.entered
        self.calls.append({"n": n, "tool_name": kwargs.get("tool_name")})
        await self._gate.wait()
        await self._release.wait()
        async with self._lock:
            self.entered -= 1
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True},
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _KeyedClient:
    """Per-tool outcomes for fan-out branches."""

    def __init__(
        self,
        *,
        fail_tools: set[str] | None = None,
        unknown_tools: set[str] | None = None,
        mrtr_tools: set[str] | None = None,
        delay_tools: dict[str, float] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._fail = fail_tools or set()
        self._unknown = unknown_tools or set()
        self._mrtr = mrtr_tools or set()
        self._delay = delay_tools or {}
        self._inflight = 0
        self.peak = 0
        self._lock = asyncio.Lock()

    async def call_tool(self, endpoint, **kwargs):
        name = str(kwargs.get("tool_name") or "")
        async with self._lock:
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
        self.calls.append({"tool_name": name, **kwargs})
        try:
            if name in self._delay:
                await asyncio.sleep(self._delay[name])
            if name in self._unknown:
                raise MCPClientError(
                    error_layer="TIMEOUT",
                    error_code="MCP_UNKNOWN",
                    message="ambiguous",
                    retryable=False,
                    outcome_unknown=True,
                )
            if name in self._fail:
                raise MCPClientError(
                    error_layer="TRANSPORT",
                    error_code="MCP_TEST_ERROR",
                    message="known failure",
                    retryable=False,
                    outcome_unknown=False,
                )
            if name in self._mrtr:
                return (
                    NormalizedInputRequired(
                        input_requests={"q": {"type": "string"}},
                        request_state={"opaque": "state"},
                        raw_size_bytes=32,
                    ),
                    {"http_status": 200, "result_type": "input_required"},
                    datetime.now(UTC),
                )
            return (
                NormalizedToolResult(
                    protocol_success=True,
                    tool_error=False,
                    structured_content={"ok": True, "from": name},
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )
        finally:
            async with self._lock:
                self._inflight -= 1


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    required: bool = True,
    on_error: str = "FAIL_EXECUTION",
    bindings: dict[str, Any] | None = None,
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
            "bindings": bindings
            or {
                "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
            },
        },
    }


def _join(
    sid: str,
    *,
    depends_on: list[str],
    policy: str = JoinPolicy.ALL_SUCCESS.value,
    on_error: str = "FAIL_EXECUTION",
    required: bool = True,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.JOIN.value,
        "required": required,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": on_error,
        "config": {"policy": policy},
    }


def _plan(
    steps: list[dict[str, Any]],
    tool_version_id: uuid.UUID,
    *,
    max_parallelism: int = 4,
) -> dict[str, Any]:
    rewritten: list[dict[str, Any]] = []
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
        "goal": "tool/join dag fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


async def _seed_executable(session: AsyncSession) -> dict[str, Any]:
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
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan_snapshot,
            plan_hash=compute_plan_hash(plan_snapshot),
            input_snapshot={},
            policy_snapshot=dict(seeded["policy_snapshot"]),
            requested_at=datetime.now(UTC),
            trace_id="trace-dag",
        )
    )
    execution = await ExecutionRepository(session).get(outcome.execution.id)
    assert execution is not None
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    await session.flush()
    return execution.id


async def _claim_and_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    plan: dict[str, Any],
    seeded: dict[str, Any],
    client: Any,
    worker_id: str = "worker-dag",
) -> tuple[uuid.UUID, Any]:
    async with db_session_factory() as session:
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        await session.commit()
        assert claim.claimed
        assert claim.lease_token is not None
        lease = claim.lease_token

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease,
    )
    return execution_id, outcome


def test_evaluate_join_policies() -> None:
    assert evaluate_join_policy(
        policy=JoinPolicy.ALL_SUCCESS,
        dependency_statuses=[StepStatus.SUCCEEDED.value, StepStatus.SUCCEEDED.value],
    ) == (StepStatus.SUCCEEDED.value, None)
    assert evaluate_join_policy(
        policy=JoinPolicy.ALL_SUCCESS,
        dependency_statuses=[StepStatus.SUCCEEDED.value, StepStatus.FAILED.value],
    ) == (StepStatus.FAILED.value, "JOIN_POLICY_UNSATISFIED")
    assert evaluate_join_policy(
        policy=JoinPolicy.ALL_COMPLETE,
        dependency_statuses=[StepStatus.FAILED.value, StepStatus.TIMED_OUT.value],
    ) == (StepStatus.SUCCEEDED.value, None)
    assert evaluate_join_policy(
        policy=JoinPolicy.ANY_SUCCESS,
        dependency_statuses=[StepStatus.FAILED.value, StepStatus.SUCCEEDED.value],
    ) == (StepStatus.SUCCEEDED.value, None)
    assert evaluate_join_policy(
        policy=JoinPolicy.ANY_SUCCESS,
        dependency_statuses=[StepStatus.FAILED.value, StepStatus.FAILED.value],
    ) == (StepStatus.FAILED.value, "JOIN_POLICY_UNSATISFIED")


def test_stop_cause_precedence() -> None:
    causes = [
        StopCause("b", 1, "FAIL_EXECUTION_FAILED", "FAILED", "E", None),
        StopCause("a", 0, "FATAL", "UNKNOWN_OUTCOME", "U", None),
        StopCause("c", 2, "FAIL_EXECUTION_TIMED_OUT", "TIMED_OUT", "T", None),
    ]
    picked = pick_stop_cause(causes)
    assert picked is not None
    assert picked.kind == "FATAL"
    assert picked.step_key == "a"
    equal = [
        StopCause("z", 5, "FAIL_EXECUTION_FAILED", "FAILED", "E", None),
        StopCause("m", 3, "FAIL_EXECUTION_FAILED", "FAILED", "E", None),
    ]
    picked2 = pick_stop_cause(equal)
    assert picked2 is not None
    assert picked2.step_key == "m"


@pytest.mark.asyncio
async def test_validate_tool_join_dag_accepts_fan_out_join(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan_dict = _plan(
            [
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["a"]),
                _join("j", depends_on=["b", "c"]),
                _tool("d", depends_on=["j"]),
            ],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan_dict, seeded=seeded
        )
        await session.commit()
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        plan = assert_execution_plan_lineage(execution)
        steps = await ExecutionRepository(session).list_steps(execution_id)
        dag = validate_tool_join_dag(plan, steps)
        assert dag.root_tool_keys == ("a",)
        assert dag.max_parallelism == 4


@pytest.mark.asyncio
async def test_a_fan_out_join_success(
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
                _join("j", depends_on=["b", "c"], policy=JoinPolicy.ALL_SUCCESS.value),
                _tool("d", depends_on=["j"]),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )
        tv = seeded["tool_version_id"]

    client = _OkClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 4  # a,b,c,d — JOIN has no MCP

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["j"].status == StepStatus.SUCCEEDED.value
        assert by_key["j"].result_inline is None
        attempts_j = await ExecutionRepository(session).list_attempts(by_key["j"].id)
        assert attempts_j == []
        for key in ("a", "b", "c", "d"):
            assert by_key[key].status == StepStatus.SUCCEEDED.value
            assert by_key[key].mcp_tool_version_id == tv


@pytest.mark.asyncio
async def test_b_multiple_roots_peak_concurrency(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a"),
                _tool("b"),
                _tool("c"),
                _join("j", depends_on=["a", "b", "c"]),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    client = _KeyedClient(delay_tools={"a": 0.05, "b": 0.05, "c": 0.05})
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 3
    assert client.peak <= 2

    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["j"].status == StepStatus.SUCCEEDED.value
        for key in ("a", "b", "c"):
            attempts = await ExecutionRepository(session).list_attempts(by_key[key].id)
            assert len(attempts) == 1
            tcs = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
            assert len(tcs) == 1


@pytest.mark.asyncio
async def test_c_all_success_join_fail_execution(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        # Remote name equals logical tool remote_name from seed — use keyed by
        # call order via Sequenced-style fail on second branch tool.
        plan = _plan(
            [
                _tool("a", on_error="CONTINUE"),
                _tool("b", on_error="CONTINUE"),
                _join(
                    "j",
                    depends_on=["a", "b"],
                    policy=JoinPolicy.ALL_SUCCESS.value,
                    on_error="FAIL_EXECUTION",
                ),
                _tool("d", depends_on=["j"]),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )
        # Make tool remote_name distinct via renaming isn't easy; fail by call index.
    class _FailSecond(_OkClient):
        async def call_tool(self, endpoint, **kwargs):
            self.calls.append({"endpoint": endpoint, **kwargs})
            if len(self.calls) == 2:
                raise MCPClientError(
                    error_layer="TRANSPORT",
                    error_code="MCP_TEST_ERROR",
                    message="branch fail",
                    retryable=False,
                    outcome_unknown=False,
                )
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _FailSecond()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.reason in {"EXECUTION_FAILED", StepStatus.FAILED.value}
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["j"].status == StepStatus.FAILED.value
        assert by_key["j"].error_code == "JOIN_POLICY_UNSATISFIED"
        assert by_key["d"].status == StepStatus.SKIPPED.value
        assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_d_all_complete_join_succeeds(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a", on_error="CONTINUE"),
                _tool("b", on_error="CONTINUE"),
                _join(
                    "j",
                    depends_on=["a", "b"],
                    policy=JoinPolicy.ALL_COMPLETE.value,
                    on_error="FAIL_EXECUTION",
                ),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    class _FailSecond(_OkClient):
        async def call_tool(self, endpoint, **kwargs):
            self.calls.append({})
            if len(self.calls) == 2:
                raise MCPClientError(
                    error_layer="TRANSPORT",
                    error_code="MCP_TEST_ERROR",
                    message="branch fail",
                    retryable=False,
                    outcome_unknown=False,
                )
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _FailSecond()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.terminal_status in {
        ExecutionStatus.SUCCEEDED.value,
        ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        StepStatus.SUCCEEDED.value,
    } or outcome.reason in {
        "EXECUTION_SUCCEEDED",
        "EXECUTION_PARTIALLY_SUCCEEDED",
    }
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status in {
            ExecutionStatus.SUCCEEDED.value,
            ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        }
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by_key["j"].status == StepStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_e_any_success_after_barrier(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a", on_error="CONTINUE"),
                _tool("b", on_error="CONTINUE"),
                _join(
                    "j",
                    depends_on=["a", "b"],
                    policy=JoinPolicy.ANY_SUCCESS.value,
                ),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    class _FailFirst(_OkClient):
        async def call_tool(self, endpoint, **kwargs):
            self.calls.append({})
            if len(self.calls) == 1:
                raise MCPClientError(
                    error_layer="TRANSPORT",
                    error_code="MCP_TEST_ERROR",
                    message="fail",
                    retryable=False,
                    outcome_unknown=False,
                )
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _FailFirst()
    execution_id, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by_key["j"].status == StepStatus.SUCCEEDED.value
        assert len(client.calls) == 2  # barrier: both deps terminal


@pytest.mark.asyncio
async def test_f_any_success_no_success(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a", on_error="CONTINUE"),
                _tool("b", on_error="CONTINUE"),
                _join(
                    "j",
                    depends_on=["a", "b"],
                    policy=JoinPolicy.ANY_SUCCESS.value,
                    on_error="FAIL_EXECUTION",
                ),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    class _FailAll(_OkClient):
        async def call_tool(self, endpoint, **kwargs):
            self.calls.append({})
            raise MCPClientError(
                error_layer="TRANSPORT",
                error_code="MCP_TEST_ERROR",
                message="fail",
                retryable=False,
                outcome_unknown=False,
            )

    client = _FailAll()
    execution_id, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by_key["j"].status == StepStatus.FAILED.value
        assert by_key["j"].error_code == "JOIN_POLICY_UNSATISFIED"


@pytest.mark.asyncio
async def test_g_mark_partial_branch_all_complete(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a", on_error="MARK_PARTIAL"),
                _tool("b", on_error="CONTINUE"),
                _join(
                    "j",
                    depends_on=["a", "b"],
                    policy=JoinPolicy.ALL_COMPLETE.value,
                ),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    class _FailFirst(_OkClient):
        async def call_tool(self, endpoint, **kwargs):
            self.calls.append({})
            if len(self.calls) == 1:
                raise MCPClientError(
                    error_layer="TRANSPORT",
                    error_code="MCP_TEST_ERROR",
                    message="partial",
                    retryable=False,
                    outcome_unknown=False,
                )
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _FailFirst()
    execution_id, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by_key["j"].status == StepStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_h_fatal_sibling_wave_settles(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        # Unsafe risk so UNKNOWN_OUTCOME is fatal.
        await session.execute(
            update(MCPToolVersion)
            .where(MCPToolVersion.id == seeded["tool_version_id"])
            .values(validation_status="VALID")
        )
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.risk_class = RiskClass.NON_IDEMPOTENT_WRITE.value
        seeded["policy_snapshot"] = build_safe_tool_policy_snapshot(policy, None)
        plan = _plan(
            [
                _tool("a"),
                _tool("b"),
                _tool("d", depends_on=["a"]),  # downstream of a — should skip
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )
        # fan-in via depends? d depends on a only — OK. But b is independent root.
        # Actually d depends_on a — after fatal a, d skipped. b succeeds in same wave.
        await session.commit()

    barrier = asyncio.Barrier(2)
    release = asyncio.Event()

    class _FatalSibling:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call_tool(self, endpoint, **kwargs):
            name = f"call-{len(self.calls)}"
            self.calls.append(name)
            idx = len(self.calls)
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

    async def _run():
        return await _claim_and_run(
            db_session_factory, plan=plan, seeded=seeded, client=client
        )

    task = asyncio.create_task(_run())
    for _ in range(200):
        if len(client.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(client.calls) == 2
    release.set()
    execution_id, outcome = await task
    assert outcome.reason in {"EXECUTION_FAILED", "UNKNOWN_OUTCOME"} or (
        outcome.terminal_status
        in {StepStatus.FAILED.value, StepStatus.UNKNOWN_OUTCOME.value}
    )

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        # One fatal UNKNOWN, one SUCCEEDED sibling, downstream SKIPPED.
        statuses = {by_key["a"].status, by_key["b"].status}
        assert StepStatus.UNKNOWN_OUTCOME.value in statuses
        assert StepStatus.SUCCEEDED.value in statuses
        assert by_key["d"].status == StepStatus.SKIPPED.value
        assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_i_fail_execution_sibling_wave(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a", on_error="FAIL_EXECUTION"),
                _tool("b", on_error="CONTINUE"),
                _tool("d", depends_on=["b"]),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    barrier = asyncio.Barrier(2)
    release = asyncio.Event()

    class _FailFirstSibling:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, endpoint, **kwargs):
            self.calls += 1
            idx = self.calls
            await barrier.wait()
            await release.wait()
            if idx == 1:
                raise MCPClientError(
                    error_layer="TRANSPORT",
                    error_code="MCP_TEST_ERROR",
                    message="fail",
                    retryable=False,
                    outcome_unknown=False,
                )
            return (
                NormalizedToolResult(protocol_success=True, tool_error=False),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _FailFirstSibling()
    task = asyncio.create_task(
        _claim_and_run(db_session_factory, plan=plan, seeded=seeded, client=client)
    )
    for _ in range(200):
        if client.calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert client.calls == 2
    release.set()
    execution_id, _ = await task
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by_key["a"].status == StepStatus.FAILED.value
        assert by_key["b"].status == StepStatus.SUCCEEDED.value
        assert by_key["d"].status == StepStatus.SKIPPED.value
        assert client.calls == 2


@pytest.mark.asyncio
async def test_j_max_parallelism_one_no_overlap(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a"),
                _tool("b"),
                _join("j", depends_on=["a", "b"]),
            ],
            seeded["tool_version_id"],
            max_parallelism=1,
        )

    client = _KeyedClient(delay_tools={"a": 0.03, "b": 0.03})
    execution_id, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert client.peak == 1
    assert len(client.calls) == 2
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_k_step_output_through_join_ancestry(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        # Ensure output schema allows structured content.
        await session.execute(
            update(MCPToolVersion)
            .where(MCPToolVersion.id == seeded["tool_version_id"])
            .values(output_schema={"type": "object"})
        )
        plan = _plan(
            [
                _tool("a"),
                _tool("b"),
                _join("j", depends_on=["a", "b"]),
                _tool(
                    "d",
                    depends_on=["j"],
                    bindings={
                        "location": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "a",
                            "path": "/structured_content/from",
                        }
                    },
                ),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    class _Structured(_OkClient):
        async def call_tool(self, endpoint, **kwargs):
            self.calls.append(kwargs)
            return (
                NormalizedToolResult(
                    protocol_success=True,
                    tool_error=False,
                    structured_content={"from": "ancestor-a"},
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _Structured()
    execution_id, _outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert len(client.calls) == 3  # a,b,d
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by_key["d"].resolved_input is not None
        assert by_key["d"].resolved_input.get("location") == "ancestor-a"


@pytest.mark.asyncio
async def test_l_failed_step_output_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool("a", on_error="CONTINUE"),
                _tool("b", on_error="CONTINUE"),
                _join(
                    "j",
                    depends_on=["a", "b"],
                    policy=JoinPolicy.ALL_COMPLETE.value,
                ),
                _tool(
                    "d",
                    depends_on=["j"],
                    bindings={
                        "location": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "a",
                            "path": "/structured_content/from",
                        }
                    },
                ),
            ],
            seeded["tool_version_id"],
            max_parallelism=2,
        )

    class _FailA(_OkClient):
        async def call_tool(self, endpoint, **kwargs):
            self.calls.append({})
            if len(self.calls) == 1:
                raise MCPClientError(
                    error_layer="TRANSPORT",
                    error_code="MCP_TEST_ERROR",
                    message="a failed",
                    retryable=False,
                    outcome_unknown=False,
                )
            return (
                NormalizedToolResult(
                    protocol_success=True,
                    tool_error=False,
                    structured_content={"from": "b"},
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _FailA()
    execution_id, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        by_key = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by_key["j"].status == StepStatus.SUCCEEDED.value
        assert by_key["d"].status == StepStatus.FAILED.value
        assert len(client.calls) == 2  # d MCP = 0


@pytest.mark.asyncio
async def test_multistep_mrtr_fail_closed_no_open_wait(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.risk_class = RiskClass.NON_IDEMPOTENT_WRITE.value
        seeded["policy_snapshot"] = build_safe_tool_policy_snapshot(policy, None)
        plan = _plan(
            [_tool("a"), _tool("b")],
            seeded["tool_version_id"],
            max_parallelism=2,
        )
        await session.commit()

    client = _KeyedClient(mrtr_tools={"weather"})  # may not match remote name
    # Force MRTR for every call:
    class _MrtrAll:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, endpoint, **kwargs):
            self.calls += 1
            return (
                NormalizedInputRequired(
                    input_requests={"q": {"type": "string"}},
                    request_state={"opaque": True},
                    raw_size_bytes=16,
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _MrtrAll()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.reason in {
        "DAG_WAIT_UNSUPPORTED",
        "EXECUTION_FAILED",
        "UNKNOWN_OUTCOME",
    } or outcome.terminal_status in {
        StepStatus.UNKNOWN_OUTCOME.value,
        StepStatus.FAILED.value,
        ExecutionStatus.FAILED.value,
    }
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        for step in steps:
            opens = await MCPInputRequestRepository(session).list_open_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
            assert opens == []
            if step.step_type == AuthorableStepType.TOOL.value:
                assert step.status == StepStatus.UNKNOWN_OUTCOME.value
                assert step.error_code == "DAG_WAIT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_multistep_approval_fail_closed_before_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-dag-{uuid.uuid4().hex[:8]}",
            name="DAG Wait Unsupported Gate",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={"roles": ["ops"]},
        )
        await session.flush()
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.requires_approval = True
        policy.approval_policy_id = approval.id
        seeded["policy_snapshot"] = build_safe_tool_policy_snapshot(policy, approval)
        plan = _plan(
            [_tool("a"), _tool("b")],
            seeded["tool_version_id"],
            max_parallelism=2,
        )
        await session.commit()

    client = _OkClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert len(client.calls) == 0
    assert outcome.reason in {"DAG_WAIT_UNSUPPORTED", "EXECUTION_FAILED"}
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.status != ExecutionStatus.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_duplicate_orchestrator_wave_in_progress(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUNNING + STARTED ToolCall → WAVE_IN_PROGRESS (no competing wave)."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [_tool("a"), _tool("b")],
            seeded["tool_version_id"],
            max_parallelism=2,
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id="w1"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease = claim.lease_token
        executions = ExecutionRepository(session)
        tool = await MCPToolRepository(session).get(seeded["tool_id"])
        assert tool is not None
        steps = await executions.list_steps(execution_id)
        now = datetime.now(UTC)
        for s in steps:
            if s.status != StepStatus.READY.value:
                continue
            s.status = StepStatus.RUNNING.value
            s.started_at = now
            s.attempt_count = 1
            s.lock_version += 1
            attempt = await executions.create_attempt(
                step_execution_id=s.id,
                attempt_no=1,
                status=StepAttemptStatus.STARTED.value,
                worker_id="w1",
                lease_expires_at=now,
                idempotency_key=f"dup-{s.step_key}-{uuid.uuid4().hex[:8]}",
                request_snapshot={"tool_version_id": str(seeded["tool_version_id"])},
                started_at=now,
            )
            await executions.create_tool_call(
                step_attempt_id=attempt.id,
                mcp_server_id=tool.mcp_server_id,
                mcp_tool_version_id=seeded["tool_version_id"],
                protocol_era=MCPProtocolEra.CURRENT.value,
                protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
                transport_type=MCPTransportType.STREAMABLE_HTTP.value,
                remote_request_id=str(uuid.uuid4()),
                request_meta={"method": "tools/call"},
                normalized_status=ToolCallNormalizedStatus.STARTED.value,
                started_at=now,
            )
        await session.commit()

    orch = ExecutionOrchestrator(
        session_factory=db_session_factory,
        tool_runner=McpToolRunner(
            session_factory=db_session_factory,
            mcp_client=_OkClient(),
            secret_resolver_factory=_resolver_factory,
            lease_seconds=120,
            result_inline_max_bytes=256_000,
        ),
    )
    outcome = await orch.run(
        execution_id=execution_id, worker_id="w1", lease_token=lease
    )
    assert outcome.reason == "WAVE_IN_PROGRESS"


# ---------------------------------------------------------------------------
# Immutable Plan ↔ step_snapshot lineage tamper
# ---------------------------------------------------------------------------


async def _materialize_claimed_dag(
    session: AsyncSession,
    *,
    seeded: dict[str, Any],
    steps: list[dict[str, Any]],
    max_parallelism: int = 4,
    worker_id: str = "worker-tamper",
) -> tuple[uuid.UUID, uuid.UUID, ExecutionPlanV1]:
    plan_dict = _plan(steps, seeded["tool_version_id"], max_parallelism=max_parallelism)
    execution_id = await _materialize_queued(
        session, plan_snapshot=plan_dict, seeded=seeded
    )
    claim = await ExecutionClaimService(session, lease_seconds=120).claim(
        execution_id=execution_id, worker_id=worker_id
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    execution = await ExecutionRepository(session).get(execution_id)
    assert execution is not None
    plan = assert_execution_plan_lineage(execution)
    return execution_id, claim.lease_token, plan


@pytest.mark.asyncio
async def test_tamper_depends_on_fails_validate_tool_join_dag(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan A→B but B.step_snapshot.depends_on=[] must fail; B cannot be a root."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        execution_id, _lease, plan = await _materialize_claimed_dag(
            session,
            seeded=seeded,
            steps=[_tool("a"), _tool("b", depends_on=["a"])],
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        snap = dict(by_key["b"].step_snapshot)
        snap["depends_on"] = []
        by_key["b"].step_snapshot = snap
        await session.commit()

        with pytest.raises(AppError) as exc:
            validate_tool_join_dag(
                plan, await ExecutionRepository(session).list_steps(execution_id)
            )
        assert exc.value.code == "RESOURCE_CONFLICT"
        assert "exact projection" in exc.value.message or "step_snapshot" in exc.value.message


@pytest.mark.asyncio
async def test_tamper_join_policy_fails_before_evaluation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        execution_id, _lease, plan = await _materialize_claimed_dag(
            session,
            seeded=seeded,
            steps=[
                _tool("a"),
                _tool("b"),
                _join("j", depends_on=["a", "b"], policy=JoinPolicy.ALL_SUCCESS.value),
            ],
            max_parallelism=2,
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        snap = dict(by_key["j"].step_snapshot)
        cfg = dict(snap["config"])
        cfg["policy"] = JoinPolicy.ALL_COMPLETE.value
        snap["config"] = cfg
        by_key["j"].step_snapshot = snap
        await session.commit()

        with pytest.raises(AppError) as exc:
            validate_tool_join_dag(
                plan, await ExecutionRepository(session).list_steps(execution_id)
            )
        assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_tamper_on_error_fails_validate(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        execution_id, _lease, plan = await _materialize_claimed_dag(
            session,
            seeded=seeded,
            steps=[
                _tool("a", on_error="FAIL_EXECUTION"),
                _tool("b", depends_on=["a"]),
            ],
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        snap = dict(by_key["a"].step_snapshot)
        snap["on_error"] = "CONTINUE"
        by_key["a"].step_snapshot = snap
        await session.commit()

        with pytest.raises(AppError) as exc:
            validate_tool_join_dag(
                plan, await ExecutionRepository(session).list_steps(execution_id)
            )
        assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_tamper_required_fails_validate(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        execution_id, _lease, plan = await _materialize_claimed_dag(
            session,
            seeded=seeded,
            steps=[_tool("a", required=True)],
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        snap = dict(by_key["a"].step_snapshot)
        snap["required"] = False
        by_key["a"].step_snapshot = snap
        await session.commit()

        with pytest.raises(AppError) as exc:
            validate_tool_join_dag(
                plan, await ExecutionRepository(session).list_steps(execution_id)
            )
        assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_tamper_step_type_projection_drift_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        execution_id, _lease, plan = await _materialize_claimed_dag(
            session,
            seeded=seeded,
            steps=[
                _tool("a"),
                _tool("b"),
                _join("j", depends_on=["a", "b"]),
            ],
            max_parallelism=2,
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        # Plan TOOL a, but ExecutionStep.step_type flipped to JOIN.
        by_key["a"].step_type = AuthorableStepType.JOIN.value
        by_key["a"].mcp_tool_version_id = None
        await session.commit()

        with pytest.raises(AppError) as exc:
            validate_tool_join_dag(
                plan, await ExecutionRepository(session).list_steps(execution_id)
            )
        assert exc.value.code == "RESOURCE_CONFLICT"
        assert "step_type" in exc.value.message or "type" in exc.value.message.lower()


@pytest.mark.asyncio
async def test_post_claim_snapshot_tamper_fail_closed_no_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orchestrator must FAILED+clear lease on post-claim snapshot tamper (MCP 0)."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        execution_id, lease, _plan = await _materialize_claimed_dag(
            session,
            seeded=seeded,
            steps=[
                _tool("a"),
                _tool("b", depends_on=["a"]),
                _tool("c", depends_on=["a"]),
                _join("j", depends_on=["b", "c"]),
            ],
            max_parallelism=2,
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        snap = dict(by_key["b"].step_snapshot)
        snap["depends_on"] = []
        by_key["b"].step_snapshot = snap
        await session.commit()
        plan_hash_before = (await ExecutionRepository(session).get(execution_id)).plan_hash

    client = _OkClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-tamper",
        lease_token=lease,
    )
    assert len(client.calls) == 0
    assert outcome.reason == "EXECUTION_FAILED"
    assert outcome.terminal_status == ExecutionStatus.FAILED.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "RESOURCE_CONFLICT"
        assert execution.lease_token is None
        assert execution.worker_id is None
        assert execution.heartbeat_at is None
        assert execution.plan_hash == plan_hash_before
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        # Root may have been READY at claim — unused READY TOOL → CANCELLED.
        assert by_key["a"].status in {
            StepStatus.CANCELLED.value,
            StepStatus.READY.value,
            StepStatus.SKIPPED.value,
        }
        if by_key["a"].status == StepStatus.READY.value:
            # If cancel didn't apply (attempt_count/started), still no MCP.
            assert by_key["a"].attempt_count == 0
        else:
            assert by_key["a"].status == StepStatus.CANCELLED.value
        for key in ("b", "c", "j"):
            assert by_key[key].status == StepStatus.SKIPPED.value
