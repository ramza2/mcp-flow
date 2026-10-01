"""PostgreSQL integration tests for flat FOR_EACH LOOP runtime (PR #50)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
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
    LoopMode,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.lineage import assert_tool_step_lineage
from app.execution.loop_reconcile import UPSTREAM_LOOP_STOPPED
from app.execution.loop_runtime import (
    LOOP_MAX_ITERATIONS_EXCEEDED,
    LOOP_TIMEOUT,
    PLAN_LIMIT_EXCEEDED,
    iteration_step_key,
)
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.orchestrator import ExecutionOrchestrator
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError
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
_AV = uuid.uuid4()
_ITEM_SCHEMA = {
    "type": "object",
    "properties": {"item": {}},
    "required": ["item"],
}


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


class _OkClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append(
            {
                k: (dict(v) if isinstance(v, dict) else v)
                for k, v in kwargs.items()
            }
        )
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={
                    "ok": True,
                    "echo": (kwargs.get("arguments") or {}).get("item"),
                },
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _FailFirstClient(_OkClient):
    async def call_tool(self, endpoint, **kwargs):
        self.calls.append(
            {
                k: (dict(v) if isinstance(v, dict) else v)
                for k, v in kwargs.items()
            }
        )
        if len(self.calls) == 1:
            raise MCPClientError(
                error_layer="TRANSPORT",
                error_code="MCP_TEST_ERROR",
                message="iter fail",
                retryable=False,
                outcome_unknown=False,
            )
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={
                    "ok": True,
                    "echo": (kwargs.get("arguments") or {}).get("item"),
                },
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


def _loop_ctx(path: str = "/item") -> dict[str, Any]:
    return {"kind": BindingKind.LOOP_CONTEXT.value, "path": path}


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    bindings: dict[str, Any] | None = None,
    when: dict[str, Any] | None = None,
    on_error: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": 30,
        "on_error": on_error,
        "config": {
            "tool_version_id": str(uuid.uuid4()),
            "bindings": bindings or {"item": _loop_ctx()},
        },
    }


def _condition(
    sid: str,
    predicate: dict[str, Any],
    *,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.CONDITION.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"predicate": predicate},
    }


def _join(
    sid: str,
    depends_on: list[str],
    *,
    policy: str = JoinPolicy.ALL_COMPLETE.value,
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


def _loop(
    sid: str,
    *,
    body_step_ids: list[str],
    max_iterations: int = 10,
    on_error: str = "FAIL_EXECUTION",
    timeout_seconds: int = 30,
    depends_on: list[str] | None = None,
    collection_path: str = "/items",
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.LOOP.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": timeout_seconds,
        "on_error": on_error,
        "config": {
            "mode": LoopMode.FOR_EACH.value,
            "max_iterations": max_iterations,
            "collection": {
                "kind": BindingKind.PLAN_INPUT.value,
                "path": collection_path,
            },
            "body_step_ids": body_step_ids,
        },
    }


def _plan(
    tool_version_id: uuid.UUID,
    steps: list[dict[str, Any]],
    *,
    max_parallelism: int = 4,
    max_steps: int | None = None,
    response_step_ids: list[str] | None = None,
) -> dict[str, Any]:
    body_ids: set[str] = set()
    for s in steps:
        if s.get("type") == AuthorableStepType.LOOP.value:
            body_ids.update((s.get("config") or {}).get("body_step_ids") or [])
    rewritten = []
    for step in steps:
        s = dict(step)
        if s["type"] == AuthorableStepType.TOOL.value:
            cfg = dict(s.get("config") or {})
            cfg["tool_version_id"] = str(tool_version_id)
            # Top-level TOOL: rewrite LOOP_CONTEXT default → LITERAL.
            if s["id"] not in body_ids and "item" in (cfg.get("bindings") or {}):
                binding = (cfg.get("bindings") or {}).get("item") or {}
                if binding.get("kind") == BindingKind.LOOP_CONTEXT.value:
                    cfg["bindings"] = {
                        "item": {
                            "kind": BindingKind.LITERAL.value,
                            "value": {"id": "top"},
                        }
                    }
            s["config"] = cfg
        rewritten.append(s)
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = max_parallelism
    if max_steps is not None:
        limits["max_steps"] = max_steps
    if response_step_ids is None:
        response_step_ids = [
            s["id"] for s in rewritten if s["id"] not in body_ids
        ][-1:]
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "pg foreach loop",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_step_ids,
        },
    }


async def _seed_loop(
    session: AsyncSession,
    *,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seeded = await _seed_ready(session)
    await session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(input_schema=input_schema or _ITEM_SCHEMA)
    )
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    return {
        **seeded,
        "policy_snapshot": build_safe_tool_policy_snapshot(policy, approval),
    }


async def _materialize_claim(
    session: AsyncSession,
    *,
    step_specs: list[dict[str, Any]],
    items: list[Any],
    worker_id: str = "pg-loop",
    max_parallelism: int = 4,
    max_steps: int | None = None,
    response_step_ids: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
    plan_override: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    seeded = await _seed_loop(session, input_schema=input_schema)
    plan = plan_override or _plan(
        seeded["tool_version_id"],
        step_specs,
        max_parallelism=max_parallelism,
        max_steps=max_steps,
        response_step_ids=response_step_ids,
    )
    # Ensure TOOL configs reference the seeded tool version.
    if plan_override is not None:
        rewritten = []
        for step in plan["steps"]:
            s = dict(step)
            if s["type"] == AuthorableStepType.TOOL.value:
                cfg = dict(s.get("config") or {})
                cfg["tool_version_id"] = str(seeded["tool_version_id"])
                s["config"] = cfg
            rewritten.append(s)
        plan = {**plan, "steps": rewritten}
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan,
            plan_hash=compute_plan_hash(plan),
            input_snapshot={"items": items},
            policy_snapshot=seeded["policy_snapshot"],
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


def _child(steps: list[Any], *, template_id: str, iteration_no: int) -> Any:
    matches = [
        s
        for s in steps
        if s.parent_step_id is not None
        and s.iteration_no == iteration_no
        and (s.step_snapshot or {}).get("id") == template_id
    ]
    assert len(matches) == 1
    return matches[0]


async def _run(
    factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    lease: uuid.UUID,
    client: Any,
    worker_id: str = "pg-loop",
) -> Any:
    runner = McpToolRunner(
        session_factory=factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    return await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease,
    )


# ---------------------------------------------------------------------------
# A–G happy / error paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_happy_path_three_items(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
            ],
            items=items,
            worker_id="pg-a",
        )
    client = _OkClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-a",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 3
    assert [c.get("arguments", {}).get("item") for c in client.calls] == items
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        assert loop.status == StepStatus.SUCCEEDED.value
        assert loop.result_inline["iterations_completed"] == 3
        children = [s for s in steps if s.parent_step_id == loop.id]
        assert len(children) == 3
        for c in children:
            assert c.step_key == iteration_step_key(
                parent_step_id=loop.id,
                iteration_no=c.iteration_no,
                template_step_id="body",
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_b_empty_collection(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
            ],
            items=[],
            worker_id="pg-b",
        )
    client = _OkClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-b",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 0
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        assert steps[0].status == StepStatus.SUCCEEDED.value
        assert steps[0].result_inline["collection_size"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c_body_dag_with_join(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["a", "b", "j"]),
                _tool("a", depends_on=["loop1"]),
                _tool("b", depends_on=["loop1"]),
                _join("j", ["a", "b"], policy=JoinPolicy.ALL_SUCCESS.value),
            ],
            items=[{"id": 1}],
            worker_id="pg-c",
            max_parallelism=2,
        )
    client = _OkClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-c",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 2
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        j = _child(steps, template_id="j", iteration_no=1)
        assert j.status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d_loop_context_and_same_iteration_step_output(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    items = [{"id": 10}, {"id": 20}]
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["a", "b"]),
                _tool("a", depends_on=["loop1"]),
                _tool(
                    "b",
                    depends_on=["a"],
                    bindings={
                        "item": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "a",
                            "path": "/structured_content/echo",
                        }
                    },
                ),
            ],
            items=items,
            worker_id="pg-d",
        )
    client = _OkClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-d",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 4
    echoed = [c.get("arguments", {}).get("item") for c in client.calls]
    assert echoed.count(items[0]) >= 2
    assert echoed.count(items[1]) >= 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e_conditional_body_when_false(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["c", "t"]),
                _condition(
                    "c",
                    {
                        "op": "eq",
                        "left": {"kind": BindingKind.LITERAL.value, "value": False},
                        "right": {"kind": BindingKind.LITERAL.value, "value": True},
                    },
                    depends_on=["loop1"],
                ),
                _tool(
                    "t",
                    depends_on=["c"],
                    when={
                        "op": "eq",
                        "left": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "c",
                            "path": "/condition_result",
                        },
                        "right": {"kind": BindingKind.LITERAL.value, "value": True},
                    },
                ),
            ],
            items=[{"id": 1}],
            worker_id="pg-e",
        )
    client = _OkClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-e",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 0
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        t = _child(steps, template_id="t", iteration_no=1)
        assert t.status == StepStatus.SKIPPED.value
        assert t.error_code == "STEP_WHEN_FALSE"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f_continue_failure_partially_succeeded(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"], on_error="CONTINUE"),
            ],
            items=items,
            worker_id="pg-f",
        )
    client = _FailFirstClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-f",
    )
    assert len(client.calls) == 3
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        assert loop.status == StepStatus.SUCCEEDED.value
        children = sorted(
            [s for s in steps if s.parent_step_id == loop.id],
            key=lambda s: s.iteration_no or 0,
        )
        assert children[0].status == StepStatus.FAILED.value
        assert children[1].status == StepStatus.SUCCEEDED.value
        assert children[2].status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_g_max_iterations_exceeded_no_body_mcp(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"], max_iterations=2),
                _tool("body", depends_on=["loop1"]),
            ],
            items=[{"id": 1}, {"id": 2}, {"id": 3}],
            worker_id="pg-g",
        )
    client = _OkClient()
    await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-g",
    )
    assert len(client.calls) == 0
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        loop = next(
            s
            for s in await ExecutionRepository(session).list_steps(execution_id)
            if s.step_key == "loop1"
        )
        assert loop.error_code == LOOP_MAX_ITERATIONS_EXCEEDED
        assert all(
            s.parent_step_id is None
            for s in await ExecutionRepository(session).list_steps(execution_id)
        )


# ---------------------------------------------------------------------------
# H–J races + lineage
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_h_duplicate_iteration_creation_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two sessions race LOOP start / iter-1 materialize — one child set."""
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
            ],
            items=[{"id": 1}, {"id": 2}],
            worker_id="pg-h",
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        assert steps[0].status == StepStatus.PENDING.value

    client = _OkClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory, tool_runner=runner
    )
    barrier = asyncio.Barrier(2)

    async def _once() -> Any:
        await barrier.wait()
        return await orch._prepare_wave(
            execution_id=execution_id,
            worker_id="pg-h",
            lease_token=lease,
        )

    r1, r2 = await asyncio.gather(_once(), _once())
    reasons = {r1.reason, r2.reason}
    assert reasons & {"WAVE_READY", "NO_READY", "LOOP_CHANGED", "STALE_LEASE"}

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        children = [s for s in steps if s.parent_step_id == loop.id]
        # Exactly one iteration-1 body row (idempotent materialize under lock).
        iter1 = [c for c in children if c.iteration_no == 1]
        assert len(iter1) == 1
        keys = [c.step_key for c in iter1]
        assert keys == [
            iteration_step_key(
                parent_step_id=loop.id, iteration_no=1, template_step_id="body"
            )
        ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_i_duplicate_child_dispatch(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Full run must not double-dispatch the same body TOOL MCP call."""
    items = [{"id": 1}]
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
            ],
            items=items,
            worker_id="pg-i",
        )
    client = _OkClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-i",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 1
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        body = _child(steps, template_id="body", iteration_no=1)
        attempts = await ExecutionRepository(session).list_attempts(body.id)
        assert len(attempts) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_j_lineage_tamper_fail_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
            ],
            items=[{"id": 1}],
            worker_id="pg-j",
        )
    client = _OkClient()
    await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-j",
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        child = _child(steps, template_id="body", iteration_no=1)
        loop.status = StepStatus.RUNNING.value

        original_key = child.step_key
        child.step_key = "tampered"
        with pytest.raises(AppError) as exc:
            assert_tool_step_lineage(execution, child, steps=steps)
        assert exc.value.code == "RESOURCE_CONFLICT"

        child.step_key = original_key
        child.iteration_no = 99
        with pytest.raises(AppError):
            assert_tool_step_lineage(execution, child, steps=steps)

        child.iteration_no = 1
        child.parent_step_id = uuid.uuid4()
        with pytest.raises(AppError):
            assert_tool_step_lineage(execution, child, steps=steps)


# ---------------------------------------------------------------------------
# Integrity gaps — multi-LOOP budget, coherent tamper, CONTINUE timeout, race
# ---------------------------------------------------------------------------

_LITERAL_X_SCHEMA = {
    "type": "object",
    "properties": {"x": {}},
    "required": ["x"],
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k_multi_loop_global_budget_fail(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    items = [{"id": i} for i in range(1, 5)]
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("l1", body_step_ids=["b1"]),
                _tool("b1", depends_on=["l1"]),
                _loop("l2", body_step_ids=["b2"]),
                _tool("b2", depends_on=["l2"]),
            ],
            items=items,
            worker_id="pg-k",
            max_steps=6,
            response_step_ids=["l1", "l2"],
        )
    client = _OkClient()
    await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-k",
    )
    assert len(client.calls) == 0
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == PLAN_LIMIT_EXCEEDED
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) <= 6


@pytest.mark.integration
@pytest.mark.asyncio
async def test_l_multi_loop_exact_fit_success(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    items = [{"id": 1}, {"id": 2}]
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("l1", body_step_ids=["b1"]),
                _tool("b1", depends_on=["l1"]),
                _loop("l2", body_step_ids=["b2"]),
                _tool("b2", depends_on=["l2"]),
            ],
            items=items,
            worker_id="pg-l",
            max_steps=6,
            response_step_ids=["l1", "l2"],
        )
    client = _OkClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-l",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 4
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 6


@pytest.mark.integration
@pytest.mark.asyncio
async def test_m_coherent_iteration_tamper_fail_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After start, coherent iteration_no+step_key retarget → FAILED, no MCP."""
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool(
                    "body",
                    depends_on=["loop1"],
                    bindings={
                        "x": {"kind": BindingKind.LITERAL.value, "value": 1}
                    },
                ),
            ],
            items=[{"id": 1}],
            worker_id="pg-m",
            input_schema=_LITERAL_X_SCHEMA,
            response_step_ids=["loop1"],
        )

    client = _OkClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory, tool_runner=runner
    )
    wave = await orch._prepare_wave(
        execution_id=execution_id, worker_id="pg-m", lease_token=lease
    )
    assert wave.reason in {"WAVE_READY", "NO_READY", "LOOP_CHANGED"}

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        child = _child(steps, template_id="body", iteration_no=1)
        child.iteration_no = 2
        child.step_key = iteration_step_key(
            parent_step_id=loop.id, iteration_no=2, template_step_id="body"
        )
        await session.commit()

    # Resume under same lease — must fail closed before Attempt/ToolCall/MCP.
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-m",
        lease_token=lease,
    )
    assert outcome.reason in {
        "EXECUTION_FAILED",
        "RESOURCE_CONFLICT",
    } or "FAIL" in (outcome.reason or "").upper()
    assert outcome.mcp_called is False
    assert len(client.calls) == 0
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        for s in steps:
            if s.step_type != AuthorableStepType.TOOL.value:
                continue
            attempts = await ExecutionRepository(session).list_attempts(s.id)
            assert attempts == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_n_continue_timeout_downstream_d(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop(
                    "L",
                    body_step_ids=["B"],
                    on_error="CONTINUE",
                    timeout_seconds=1,
                ),
                _tool("B", depends_on=["L"]),
                _tool(
                    "D",
                    depends_on=["L"],
                    bindings={
                        "item": {
                            "kind": BindingKind.LITERAL.value,
                            "value": {"id": "downstream"},
                        }
                    },
                ),
            ],
            items=[{"id": 1}, {"id": 2}],
            worker_id="pg-n",
            response_step_ids=["L", "D"],
        )

    client = _OkClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory, tool_runner=runner
    )
    wave1 = await orch._prepare_wave(
        execution_id=execution_id, worker_id="pg-n", lease_token=lease
    )
    assert wave1.reason in {"WAVE_READY", "NO_READY", "LOOP_CHANGED"}
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.RUNNING.value
        loop.started_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

    await orch._prepare_wave(
        execution_id=execution_id, worker_id="pg-n", lease_token=lease
    )
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.TIMED_OUT.value
        assert loop.error_code == LOOP_TIMEOUT
        body = next(
            s for s in steps if (s.step_snapshot or {}).get("id") == "B"
        )
        assert body.status in {
            StepStatus.SKIPPED.value,
            StepStatus.CANCELLED.value,
        }
        assert body.error_code == UPSTREAM_LOOP_STOPPED
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.lease_token is not None
        lease = execution.lease_token

    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-n",
        lease_token=lease,
    )
    assert outcome.reason == "EXECUTION_PARTIALLY_SUCCEEDED"
    assert len(client.calls) == 1
    assert client.calls[0].get("arguments", {}).get("item") == {
        "id": "downstream"
    }
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        d = next(s for s in steps if s.step_key == "D")
        assert d.status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_o_next_iteration_materialize_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Race two _prepare_wave at n→n+1 after iter1 complete — one iter2 set."""
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
            ],
            items=[{"id": 1}, {"id": 2}],
            worker_id="pg-o",
        )

    client = _OkClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory, tool_runner=runner
    )
    # Start LOOP + materialize iter1.
    await orch._prepare_wave(
        execution_id=execution_id, worker_id="pg-o", lease_token=lease
    )
    # Terminalize iter1 body without MCP so next prepare advances n→n+1.
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        assert loop.status == StepStatus.RUNNING.value
        body = _child(steps, template_id="body", iteration_no=1)
        body.status = StepStatus.SUCCEEDED.value
        body.finished_at = datetime.now(UTC)
        body.result_inline = {"structured_content": {"ok": True}}
        body.lock_version += 1
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def _once() -> Any:
        await barrier.wait()
        return await orch._prepare_wave(
            execution_id=execution_id,
            worker_id="pg-o",
            lease_token=lease,
        )

    r1, r2 = await asyncio.gather(_once(), _once())
    reasons = {r1.reason, r2.reason}
    assert reasons & {"WAVE_READY", "NO_READY", "LOOP_CHANGED", "STALE_LEASE"}

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        iter2 = [
            s
            for s in steps
            if s.parent_step_id == loop.id and s.iteration_no == 2
        ]
        assert len(iter2) == 1
        assert iter2[0].step_key == iteration_step_key(
            parent_step_id=loop.id, iteration_no=2, template_step_id="body"
        )
        # Complete set for single-body LOOP.
        assert (iter2[0].step_snapshot or {}).get("id") == "body"
