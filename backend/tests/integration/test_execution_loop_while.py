"""PostgreSQL integration tests for flat WHILE LOOP runtime (PR #51)."""

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
    LoopMode,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.lineage import assert_tool_step_lineage
from app.execution.loop_runtime import (
    LOOP_MAX_ITERATIONS_EXCEEDED,
    PLAN_LIMIT_EXCEEDED,
    iteration_step_key,
    parse_while_predicate_history,
)
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.orchestrator import ExecutionOrchestrator
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

_AV = uuid.uuid4()
_VALUE_SCHEMA = {
    "type": "object",
    "properties": {"value": {}},
    "required": ["value"],
}
_ITEM_SCHEMA = {
    "type": "object",
    "properties": {"item": {}},
    "required": ["item"],
}


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


class _ValueClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        args = kwargs.get("arguments") or {}
        self.calls.append(
            {k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()}
        )
        raw = args.get("value", args.get("item"))
        if isinstance(raw, dict):
            val = raw.get("value", raw.get("id", 0))
        else:
            val = raw
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"value": val, "ok": True},
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


def _lc(path: str) -> dict[str, Any]:
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
            "bindings": bindings or {"value": _lc("/iteration_no")},
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


def _join(sid: str, depends_on: list[str]) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.JOIN.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"policy": JoinPolicy.ALL_SUCCESS.value},
    }


def _while(
    sid: str,
    *,
    body_step_ids: list[str],
    predicate: dict[str, Any],
    max_iterations: int = 10,
    on_error: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.LOOP.value,
        "required": True,
        "depends_on": [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": on_error,
        "config": {
            "mode": LoopMode.WHILE.value,
            "max_iterations": max_iterations,
            "predicate": predicate,
            "body_step_ids": body_step_ids,
        },
    }


def _foreach(
    sid: str,
    *,
    body_step_ids: list[str],
    collection_path: str = "/items",
    max_iterations: int = 10,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.LOOP.value,
        "required": True,
        "depends_on": [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
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


def _prev_lt(template_id: str = "B", limit: int = 3) -> dict[str, Any]:
    return {
        "op": "or",
        "children": [
            {"op": "is_null", "operand": _lc("/previous_iteration")},
            {
                "op": "lt",
                "left": _lc(
                    f"/previous_iteration/steps/{template_id}"
                    "/result_inline/structured_content/value"
                ),
                "right": {"kind": BindingKind.LITERAL.value, "value": limit},
            },
        ],
    }


def _always_true() -> dict[str, Any]:
    return {
        "op": "eq",
        "left": {"kind": BindingKind.LITERAL.value, "value": True},
        "right": {"kind": BindingKind.LITERAL.value, "value": True},
    }


def _always_false() -> dict[str, Any]:
    return {
        "op": "eq",
        "left": {"kind": BindingKind.LITERAL.value, "value": True},
        "right": {"kind": BindingKind.LITERAL.value, "value": False},
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
        "goal": "pg while loop",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_step_ids,
        },
    }


async def _seed(
    session: AsyncSession, *, input_schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    seeded = await _seed_ready(session)
    await session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(input_schema=input_schema or _VALUE_SCHEMA)
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
    worker_id: str = "pg-while",
    max_parallelism: int = 4,
    max_steps: int | None = None,
    response_step_ids: list[str] | None = None,
    input_snapshot: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    seeded = await _seed(session, input_schema=input_schema)
    plan = _plan(
        seeded["tool_version_id"],
        step_specs,
        max_parallelism=max_parallelism,
        max_steps=max_steps,
        response_step_ids=response_step_ids,
    )
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan,
            plan_hash=compute_plan_hash(plan),
            input_snapshot=input_snapshot if input_snapshot is not None else {},
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
    worker_id: str = "pg-while",
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_zero_iteration(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _while("L", body_step_ids=["B"], predicate=_always_false()),
                _tool("B", depends_on=["L"]),
            ],
            response_step_ids=["L"],
            worker_id="pg-a",
        )
    client = _ValueClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=eid,
        lease=lease,
        client=client,
        worker_id="pg-a",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 0
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SUCCEEDED.value
        assert loop.result_inline == {
            "mode": "WHILE",
            "iterations_completed": 0,
        }
        assert not [s for s in steps if s.parent_step_id == loop.id]
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_b_c_three_iteration_previous_and_body_context(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _while(
                    "L",
                    body_step_ids=["B"],
                    predicate=_prev_lt("B", 3),
                ),
                _tool(
                    "B",
                    depends_on=["L"],
                    bindings={
                        "value": _lc("/iteration_no"),
                    },
                ),
            ],
            response_step_ids=["L"],
            worker_id="pg-b",
        )
    client = _ValueClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=eid,
        lease=lease,
        client=client,
        worker_id="pg-b",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 3
    # Body received iteration_no 1,2,3
    values = [
        (c.get("arguments") or {}).get("value") for c in client.calls
    ]
    assert values == [1, 2, 3]
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.result_inline == {
            "mode": "WHILE",
            "iterations_completed": 3,
        }
        hist = parse_while_predicate_history(loop.resolved_input)
        assert [e["result"] for e in hist["predicate_history"]] == [
            True,
            True,
            True,
            False,
        ]
        for n in (1, 2, 3):
            b = _child(steps, template_id="B", iteration_no=n)
            assert b.status == StepStatus.SUCCEEDED.value
            assert b.result_inline["structured_content"]["value"] == n


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d_same_iteration_dag(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _while(
                    "L",
                    body_step_ids=["A", "C", "B", "J"],
                    predicate=_prev_lt("A", 1),
                ),
                _tool("A", depends_on=["L"], bindings={"value": _lc("/iteration_no")}),
                _condition(
                    "C",
                    {
                        "op": "eq",
                        "left": _lc("/iteration_no"),
                        "right": {"kind": BindingKind.LITERAL.value, "value": 1},
                    },
                    depends_on=["A"],
                ),
                _tool(
                    "B",
                    depends_on=["C"],
                    when={
                        "op": "eq",
                        "left": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "C",
                            "path": "/condition_result",
                        },
                        "right": {"kind": BindingKind.LITERAL.value, "value": True},
                    },
                    bindings={"value": _lc("/index")},
                ),
                _join("J", ["A", "B"]),
            ],
            response_step_ids=["L"],
            max_parallelism=2,
            worker_id="pg-d",
        )
    client = _ValueClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=eid,
        lease=lease,
        client=client,
        worker_id="pg-d",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 2
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        j = _child(steps, template_id="J", iteration_no=1)
        assert j.status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e_max_iterations_boundary_continue(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _while(
                    "L",
                    body_step_ids=["B"],
                    predicate=_always_true(),
                    max_iterations=2,
                    on_error="CONTINUE",
                ),
                _tool("B", depends_on=["L"]),
                _tool(
                    "D",
                    depends_on=["L"],
                    bindings={
                        "value": {"kind": BindingKind.LITERAL.value, "value": 99}
                    },
                ),
            ],
            response_step_ids=["L", "D"],
            worker_id="pg-e",
        )
    client = _ValueClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=eid,
        lease=lease,
        client=client,
        worker_id="pg-e",
    )
    assert outcome.reason == "EXECUTION_PARTIALLY_SUCCEEDED"
    assert len(client.calls) == 3
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.FAILED.value
        assert loop.error_code == LOOP_MAX_ITERATIONS_EXCEEDED
        hist = parse_while_predicate_history(loop.resolved_input)
        assert hist["predicate_history"][-1] == {
            "next_iteration_no": 3,
            "evidence_hash": hist["predicate_history"][-1]["evidence_hash"],
            "result": True,
        }
        assert not any(s.iteration_no == 3 for s in steps)
        d = next(s for s in steps if s.step_key == "D")
        assert d.status == StepStatus.SUCCEEDED.value
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f_max_steps_actual_expansion(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _while(
                    "L",
                    body_step_ids=["B"],
                    predicate=_always_true(),
                    max_iterations=5,
                ),
                _tool("B", depends_on=["L"]),
            ],
            response_step_ids=["L"],
            max_steps=2,
            worker_id="pg-f",
        )
    client = _ValueClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=eid,
        lease=lease,
        client=client,
        worker_id="pg-f",
    )
    assert outcome.reason == "EXECUTION_FAILED"
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.error_code == PLAN_LIMIT_EXCEEDED
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(eid)
        assert len(steps) <= 2
    assert len(client.calls) <= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_g_h_i_duplicate_gate_races(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # G: duplicate initial gate
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _while("L", body_step_ids=["B"], predicate=_always_true(), max_iterations=2),
                _tool("B", depends_on=["L"]),
            ],
            response_step_ids=["L"],
            worker_id="pg-g",
        )
    client = _ValueClient()
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
            execution_id=eid, worker_id="pg-g", lease_token=lease
        )

    await asyncio.gather(_once(), _once())
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        hist = parse_while_predicate_history(loop.resolved_input)
        assert len([e for e in hist["predicate_history"] if e["next_iteration_no"] == 1]) == 1
        iter1 = [s for s in steps if s.parent_step_id == loop.id and s.iteration_no == 1]
        assert len(iter1) == 1
        assert iter1[0].step_key == iteration_step_key(
            parent_step_id=loop.id, iteration_no=1, template_step_id="B"
        )

    # H: duplicate next gate after iter1 terminal (manual settle, no MCP).
    async with integration_session_factory() as session:
        eid2, lease2, _ = await _materialize_claim(
            session,
            step_specs=[
                _while(
                    "L",
                    body_step_ids=["B"],
                    predicate=_prev_lt("B", 3),
                ),
                _tool("B", depends_on=["L"]),
            ],
            response_step_ids=["L"],
            worker_id="pg-h",
        )
    orch2 = ExecutionOrchestrator(
        session_factory=integration_session_factory,
        tool_runner=McpToolRunner(
            session_factory=integration_session_factory,
            mcp_client=_ValueClient(),
            secret_resolver_factory=_resolver_factory,
            lease_seconds=120,
            result_inline_max_bytes=256_000,
        ),
    )
    await orch2._prepare_wave(
        execution_id=eid2, worker_id="pg-h", lease_token=lease2
    )
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid2)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.RUNNING.value
        b1 = _child(steps, template_id="B", iteration_no=1)
        b1.status = StepStatus.SUCCEEDED.value
        b1.finished_at = datetime.now(UTC)
        b1.result_inline = {"structured_content": {"value": 1, "ok": True}}
        b1.lock_version += 1
        await session.commit()

    barrier2 = asyncio.Barrier(2)

    async def _next() -> Any:
        await barrier2.wait()
        return await orch2._prepare_wave(
            execution_id=eid2, worker_id="pg-h", lease_token=lease2
        )

    await asyncio.gather(_next(), _next())
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid2)
        loop = next(s for s in steps if s.step_key == "L")
        hist = parse_while_predicate_history(loop.resolved_input)
        gate2 = [e for e in hist["predicate_history"] if e["next_iteration_no"] == 2]
        assert len(gate2) == 1
        assert gate2[0]["result"] is True
        iter2 = [s for s in steps if s.parent_step_id == loop.id and s.iteration_no == 2]
        assert len(iter2) == 1

    # I: false-gate race after final iteration — full run to completion once
    async with integration_session_factory() as session:
        eid3, lease3, _ = await _materialize_claim(
            session,
            step_specs=[
                _while(
                    "L",
                    body_step_ids=["B"],
                    predicate=_prev_lt("B", 1),
                ),
                _tool("B", depends_on=["L"]),
            ],
            response_step_ids=["L"],
            worker_id="pg-i",
        )
    client3 = _ValueClient()
    outcome3 = await _run(
        integration_session_factory,
        execution_id=eid3,
        lease=lease3,
        client=client3,
        worker_id="pg-i",
    )
    assert outcome3.reason == "EXECUTION_SUCCEEDED"
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid3)
        loop = next(s for s in steps if s.step_key == "L")
        hist = parse_while_predicate_history(loop.resolved_input)
        false_gates = [e for e in hist["predicate_history"] if e["result"] is False]
        assert len(false_gates) == 1
        assert false_gates[0]["next_iteration_no"] == 2
        assert loop.status == StepStatus.SUCCEEDED.value
        assert len([s for s in steps if s.parent_step_id == loop.id]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_j_history_evidence_drift(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _while(
                    "L",
                    body_step_ids=["B"],
                    predicate=_prev_lt("B", 3),
                ),
                _tool("B", depends_on=["L"]),
            ],
            response_step_ids=["L"],
            worker_id="pg-j",
        )
    client = _ValueClient()
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
    # Gate1 + materialize iter1.
    await orch._prepare_wave(
        execution_id=eid, worker_id="pg-j", lease_token=lease
    )
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        b1 = _child(steps, template_id="B", iteration_no=1)
        b1.status = StepStatus.SUCCEEDED.value
        b1.finished_at = datetime.now(UTC)
        b1.result_inline = {"structured_content": {"value": 1, "ok": True}}
        b1.lock_version += 1
        await session.commit()
    # Gate2 true + materialize iter2 (no MCP yet).
    await orch._prepare_wave(
        execution_id=eid, worker_id="pg-j", lease_token=lease
    )
    # Tamper iter1 durable result after gate2 pinned.
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        b1 = _child(steps, template_id="B", iteration_no=1)
        b1.result_inline = {
            "structured_content": {"value": 999, "tampered": True}
        }
        b1.lock_version += 1
        await session.commit()

    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        b2 = _child(steps, template_id="B", iteration_no=2)
        with pytest.raises(AppError) as exc:
            assert_tool_step_lineage(execution, b2, steps=steps)
        assert exc.value.code == "RESOURCE_CONFLICT"
        assert b2.attempt_count == 0
        attempts = await ExecutionRepository(session).list_attempts(b2.id)
        assert len(attempts) == 0
    assert len(client.calls) == 0

    # Orchestrated dispatch must also fail closed (no new MCP / Attempt).
    try:
        await runner.run_claimed_execution(
            execution_id=eid, worker_id="pg-j", lease_token=lease
        )
    except Exception:
        pass
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        b2 = _child(steps, template_id="B", iteration_no=2)
        attempts = await ExecutionRepository(session).list_attempts(b2.id)
        assert len(attempts) == 0
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.lease_token is None or execution.status == (
            ExecutionStatus.FAILED.value
        )
    assert len(client.calls) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k_mixed_foreach_while(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Dual-schema TOOL: accept either item or value.
    dual_schema = {
        "type": "object",
        "properties": {"item": {}, "value": {}},
    }
    async with integration_session_factory() as session:
        eid, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _foreach("FE", body_step_ids=["fb"], max_iterations=3),
                _tool(
                    "fb",
                    depends_on=["FE"],
                    bindings={
                        "item": {
                            "kind": BindingKind.LOOP_CONTEXT.value,
                            "path": "/item",
                        }
                    },
                ),
                _while(
                    "W",
                    body_step_ids=["wb"],
                    predicate=_prev_lt("wb", 2),
                    max_iterations=5,
                ),
                _tool(
                    "wb",
                    depends_on=["W"],
                    bindings={"value": _lc("/iteration_no")},
                ),
            ],
            response_step_ids=["FE", "W"],
            input_snapshot={"items": [{"id": 1}, {"id": 2}]},
            input_schema=dual_schema,
            max_parallelism=2,
            max_steps=20,
            worker_id="pg-k",
        )
    client = _ValueClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=eid,
        lease=lease,
        client=client,
        worker_id="pg-k",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        assert len(steps) <= 20
        fe = next(s for s in steps if s.step_key == "FE")
        w = next(s for s in steps if s.step_key == "W")
        assert fe.result_inline == {
            "mode": "FOR_EACH",
            "iterations_completed": 2,
            "collection_size": 2,
        }
        assert w.result_inline == {
            "mode": "WHILE",
            "iterations_completed": 2,
        }
        # FOR_EACH context unchanged — children have item, no previous_iteration
        # in FOR_EACH resolved_input
        assert set(fe.resolved_input.keys()) == {
            "mode",
            "collection_hash",
            "collection_size",
        }
        assert "previous_iteration" not in (fe.resolved_input or {})
        parse_while_predicate_history(w.resolved_input)
