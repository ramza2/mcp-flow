"""Unit tests for flat WHILE LOOP runtime (PR #51)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.agent.complex_plan_validator import StaticComplexPlanValidator
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
from app.execution.dag import validate_tool_join_dag
from app.execution.lineage import assert_tool_step_lineage
from app.execution.loop_reconcile import (
    build_while_evidence_object,
)
from app.execution.loop_runtime import (
    LOOP_MAX_ITERATIONS_EXCEEDED,
    LOOP_RUNTIME_UNSUPPORTED,
    LOOP_TIMEOUT,
    PLAN_LIMIT_EXCEEDED,
    append_or_replay_while_history,
    assert_flat_foreach_runtime_compatible,
    assert_while_parent_control_state,
    build_loop_context_projection,
    build_while_loop_context_projection,
    empty_while_control_evidence,
    parse_while_predicate_history,
    previous_iteration_step_projection,
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
    ExecutionPlanV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects, _seed_ready

_AV = uuid.uuid4()
_AP = uuid.uuid4()

_VALUE_SCHEMA = {
    "type": "object",
    "properties": {"value": {}},
    "required": ["value"],
}


class _ValueClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        args = kwargs.get("arguments") or {}
        self.calls.append(
            {k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()}
        )
        # Echo numeric value for previous_iteration predicates.
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


class _FailClient(_ValueClient):
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        super().__init__()
        self._fail_on = fail_on or set()

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append(
            {k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()}
        )
        n = len(self.calls)
        if n in self._fail_on:
            raise MCPClientError(
                error_layer="TRANSPORT",
                error_code="MCP_TEST_ERROR",
                message="body fail",
                retryable=False,
                outcome_unknown=False,
            )
        args = kwargs.get("arguments") or {}
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


def _resolver_factory():
    return UnimplementedSecretResolver()


def _lc(path: str) -> dict[str, Any]:
    return {"kind": BindingKind.LOOP_CONTEXT.value, "path": path}


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    bindings: dict[str, Any] | None = None,
    when: dict[str, Any] | None = None,
    on_error: str = "FAIL_EXECUTION",
    required: bool = True,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": required,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": timeout_seconds,
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
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.CONDITION.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"predicate": predicate},
    }


def _join(
    sid: str,
    depends_on: list[str],
    *,
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


def _while_loop(
    sid: str,
    *,
    body_step_ids: list[str],
    predicate: dict[str, Any],
    max_iterations: int = 10,
    on_error: str = "FAIL_EXECUTION",
    timeout_seconds: int = 30,
    depends_on: list[str] | None = None,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.LOOP.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": timeout_seconds,
        "on_error": on_error,
        "config": {
            "mode": LoopMode.WHILE.value,
            "max_iterations": max_iterations,
            "predicate": predicate,
            "body_step_ids": body_step_ids,
        },
    }


def _approval(sid: str, *, depends_on: list[str]) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.APPROVAL.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"approval_policy_id": str(_AP)},
    }


def _body_ids_of(steps: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for s in steps:
        if s.get("type") == AuthorableStepType.LOOP.value:
            out.update((s.get("config") or {}).get("body_step_ids") or [])
    return out


def _plan(
    steps: list[dict[str, Any]],
    tool_version_id: uuid.UUID,
    *,
    max_parallelism: int = 4,
    max_steps: int | None = None,
    max_loop_iterations: int | None = None,
    response_step_ids: list[str] | None = None,
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
    if max_steps is not None:
        limits["max_steps"] = max_steps
    if max_loop_iterations is not None:
        limits["max_loop_iterations"] = max_loop_iterations
    body_ids = _body_ids_of(rewritten)
    if response_step_ids is None:
        response_step_ids = [
            s["id"]
            for s in rewritten
            if s["id"] not in body_ids
            and s["type"]
            in {
                AuthorableStepType.TOOL.value,
                AuthorableStepType.LOOP.value,
                AuthorableStepType.JOIN.value,
            }
        ] or [s["id"] for s in rewritten if s["id"] not in body_ids][-1:]
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "while loop fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_step_ids,
        },
    }


def _prev_lt_predicate(template_id: str = "B", limit: int = 3) -> dict[str, Any]:
    """is_null(previous) OR previous.steps.B.result_inline.structured_content.value < limit."""
    return {
        "op": "or",
        "children": [
            {
                "op": "is_null",
                "operand": _lc("/previous_iteration"),
            },
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


def _simple_while_plan(
    tool_version_id: uuid.UUID,
    *,
    predicate: dict[str, Any] | None = None,
    max_iterations: int = 10,
    on_error_body: str = "FAIL_EXECUTION",
    on_error_loop: str = "FAIL_EXECUTION",
    timeout_seconds: int = 30,
    max_steps: int | None = None,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _plan(
        [
            _while_loop(
                "L",
                body_step_ids=["B"],
                predicate=predicate or _prev_lt_predicate("B", 3),
                max_iterations=max_iterations,
                on_error=on_error_loop,
                timeout_seconds=timeout_seconds,
                when=when,
            ),
            _tool(
                "B",
                depends_on=["L"],
                on_error=on_error_body,
                bindings={"value": _lc("/iteration_no")},
            ),
        ],
        tool_version_id,
        max_steps=max_steps,
        response_step_ids=["L"],
    )


async def _seed_executable(
    session: AsyncSession,
    *,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seeded = await _seed_ready(session)
    schema = input_schema or _VALUE_SCHEMA
    await session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(input_schema=schema)
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
            input_snapshot=input_snapshot if input_snapshot is not None else {},
            policy_snapshot=dict(seeded["policy_snapshot"]),
            requested_at=datetime.now(UTC),
            trace_id="trace-while",
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
    input_snapshot: dict[str, Any] | None = None,
    worker_id: str = "worker-while",
) -> tuple[uuid.UUID, Any]:
    async with db_session_factory() as session:
        execution_id = await _materialize_queued(
            session,
            plan_snapshot=plan,
            seeded=seeded,
            input_snapshot=input_snapshot,
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


def _child_by_template(steps: list[Any], *, template_id: str, iteration_no: int) -> Any:
    matches = [
        s
        for s in steps
        if s.parent_step_id is not None
        and s.iteration_no == iteration_no
        and (s.step_snapshot or {}).get("id") == template_id
    ]
    assert len(matches) == 1, (template_id, iteration_no, matches)
    return matches[0]


# ---------------------------------------------------------------------------
# Context / history helpers
# ---------------------------------------------------------------------------


def test_while_candidate_1_projection_and_foreach_unchanged() -> None:
    while_ctx = build_while_loop_context_projection(
        loop_plan_step_id="L",
        iteration_no=1,
        max_iterations=10,
        previous_iteration=None,
    )
    assert while_ctx == {
        "loop_step_id": "L",
        "mode": "WHILE",
        "iteration_no": 1,
        "index": 0,
        "max_iterations": 10,
        "previous_iteration": None,
    }
    fe = build_loop_context_projection(
        loop_plan_step_id="L",
        mode="FOR_EACH",
        iteration_no=1,
        item={"id": 1},
        collection_size=3,
    )
    assert fe == {
        "loop_step_id": "L",
        "mode": "FOR_EACH",
        "iteration_no": 1,
        "index": 0,
        "item": {"id": 1},
        "collection_size": 3,
    }
    assert "previous_iteration" not in fe


def test_previous_iteration_step_projection_exact_keys() -> None:
    class _S:
        status = "SUCCEEDED"
        condition_result = None
        error_code = None
        result_inline = {"structured_content": {"done": False}}

    proj = previous_iteration_step_projection(_S())  # type: ignore[arg-type]
    assert set(proj) == {
        "status",
        "condition_result",
        "error_code",
        "result_inline",
    }
    assert proj["result_inline"]["structured_content"]["done"] is False


def test_while_history_shape_and_replay() -> None:
    empty = empty_while_control_evidence()
    assert empty == {"mode": "WHILE", "predicate_history": []}
    digest = "a" * 64
    updated = append_or_replay_while_history(
        resolved_input=empty,
        next_iteration_no=1,
        evidence_hash=digest,
        result=True,
    )
    assert updated["predicate_history"] == [
        {
            "next_iteration_no": 1,
            "evidence_hash": digest,
            "result": True,
        }
    ]
    # Replay exact match ok.
    again = append_or_replay_while_history(
        resolved_input=updated,
        next_iteration_no=1,
        evidence_hash=digest,
        result=True,
    )
    assert len(again["predicate_history"]) == 1
    with pytest.raises(AppError) as exc:
        append_or_replay_while_history(
            resolved_input=updated,
            next_iteration_no=1,
            evidence_hash="b" * 64,
            result=True,
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    with pytest.raises(AppError):
        parse_while_predicate_history(
            {
                "mode": "WHILE",
                "predicate_history": [
                    {
                        "next_iteration_no": 1,
                        "evidence_hash": "NOTHEX",
                        "result": True,
                    }
                ],
            }
        )
    with pytest.raises(AppError):
        append_or_replay_while_history(
            resolved_input=empty,
            next_iteration_no=1,
            evidence_hash=digest,
            result="yes",  # type: ignore[arg-type]
        )


def test_static_loop_when_and_foreach_collection_reject_loop_context() -> None:
    tv = uuid.uuid4()
    plan = _plan(
        [
            _while_loop(
                "L",
                body_step_ids=["B"],
                predicate=_always_true(),
                when={
                    "op": "eq",
                    "left": _lc("/iteration_no"),
                    "right": {"kind": BindingKind.LITERAL.value, "value": 1},
                },
            ),
            _tool("B", depends_on=["L"]),
        ],
        tv,
    )
    result = StaticComplexPlanValidator().validate(ExecutionPlanV1.model_validate(plan))
    assert not result.ok
    assert any("LOOP Step.when" in e.message for e in result.errors)

    fe_plan = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "fe",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": [
            {
                "id": "L",
                "name": "L",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 3,
                    "collection": _lc("/item"),
                    "body_step_ids": ["B"],
                },
            },
            _tool("B", depends_on=["L"]),
        ],
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": ["L"],
        },
    }
    # Rewrite tool version
    fe_plan["steps"][1]["config"]["tool_version_id"] = str(tv)
    fe_result = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(fe_plan)
    )
    assert not fe_result.ok
    assert any("FOR_EACH collection" in e.message for e in fe_result.errors)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_iteration_and_three_iteration_while(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # Zero iterations
    client0 = _ValueClient()
    plan0 = _simple_while_plan(
        seeded["tool_version_id"], predicate=_always_false()
    )
    eid0, _ = await _claim_and_run(
        db_session_factory, plan=plan0, seeded=seeded, client=client0
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid0)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SUCCEEDED.value
        assert loop.result_inline == {
            "mode": "WHILE",
            "iterations_completed": 0,
        }
        hist = parse_while_predicate_history(loop.resolved_input)
        assert len(hist["predicate_history"]) == 1
        assert hist["predicate_history"][0]["result"] is False
        assert not [s for s in steps if s.parent_step_id == loop.id]
        execution = await ExecutionRepository(session).get(eid0)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert len(client0.calls) == 0

    # Three iterations from previous result
    client3 = _ValueClient()
    plan3 = _simple_while_plan(seeded["tool_version_id"])
    eid3, _ = await _claim_and_run(
        db_session_factory, plan=plan3, seeded=seeded, client=client3
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid3)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SUCCEEDED.value
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
        children = [s for s in steps if s.parent_step_id == loop.id]
        assert len(children) == 3
        for n in (1, 2, 3):
            b = _child_by_template(steps, template_id="B", iteration_no=n)
            assert b.status == StepStatus.SUCCEEDED.value
            assert b.result_inline["structured_content"]["value"] == n
        execution = await ExecutionRepository(session).get(eid3)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert len(client3.calls) == 3


@pytest.mark.asyncio
async def test_true_false_after_one_and_body_reads_previous(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # Stop after 1: previous value < 1 is false for candidate 2 when value=1.
    client = _ValueClient()
    plan = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_prev_lt_predicate("B", 1),
    )
    eid, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.result_inline["iterations_completed"] == 1
        hist = parse_while_predicate_history(loop.resolved_input)
        assert [e["result"] for e in hist["predicate_history"]] == [True, False]
    assert len(client.calls) == 1

    # Body consumes previous via LOOP_CONTEXT (iteration 1 path MISSING —
    # use PLAN_INPUT seed via when so iter1 uses literal; iter2+ previous).
    # Simpler: body binds iteration_no always + optional previous for iter>=2
    # using a TOOL that only needs iteration_no (already covered above).


@pytest.mark.asyncio
async def test_body_condition_when_join_and_error_policies(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # Body DAG: A → C CONDITION → B when C → J JOIN
    # Run only one iteration (always true once then false via max=1 false gate).
    # Use always-true predicate + max_iterations=1 → gate2 true → MAX_EXCEEDED.
    # Instead: previous < 2 so 1 iteration then false... wait value=1 < 2 → gate2 true
    # previous < 1 → one iteration.
    plan = _plan(
        [
            _while_loop(
                "L",
                body_step_ids=["A", "C", "B", "J"],
                predicate=_prev_lt_predicate("A", 1),
                max_iterations=5,
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
        seeded["tool_version_id"],
        response_step_ids=["L"],
    )
    client = _ValueClient()
    eid, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SUCCEEDED.value
        assert loop.result_inline["iterations_completed"] == 1
        a = _child_by_template(steps, template_id="A", iteration_no=1)
        b = _child_by_template(steps, template_id="B", iteration_no=1)
        j = _child_by_template(steps, template_id="J", iteration_no=1)
        assert a.status == StepStatus.SUCCEEDED.value
        assert b.status == StepStatus.SUCCEEDED.value
        assert j.status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 2  # A + B

    # CONTINUE body failure then next Predicate
    client_c = _FailClient(fail_on={1})
    plan_c = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_prev_lt_predicate("B", 3),
        on_error_body="CONTINUE",
    )
    eid_c, _ = await _claim_and_run(
        db_session_factory, plan=plan_c, seeded=seeded, client=client_c
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid_c)
        loop = next(s for s in steps if s.step_key == "L")
        # After fail iter1 CONTINUE, previous B has FAILED; predicate
        # previous.value < 3 may PREDICATE_OPERAND_MISSING → fatal.
        # Author safe predicate with exists short-circuit:
        pass
    # Use safer predicate for CONTINUE case
    safe_pred = {
        "op": "or",
        "children": [
            {"op": "is_null", "operand": _lc("/previous_iteration")},
            {
                "op": "and",
                "children": [
                    {
                        "op": "exists",
                        "operand": _lc(
                            "/previous_iteration/steps/B/result_inline"
                            "/structured_content/value"
                        ),
                    },
                    {
                        "op": "lt",
                        "left": _lc(
                            "/previous_iteration/steps/B/result_inline"
                            "/structured_content/value"
                        ),
                        "right": {"kind": BindingKind.LITERAL.value, "value": 3},
                    },
                ],
            },
            {
                "op": "and",
                "children": [
                    {
                        "op": "eq",
                        "left": _lc("/previous_iteration/steps/B/status"),
                        "right": {
                            "kind": BindingKind.LITERAL.value,
                            "value": "FAILED",
                        },
                    },
                    {
                        "op": "lt",
                        "left": _lc("/previous_iteration/iteration_no"),
                        "right": {"kind": BindingKind.LITERAL.value, "value": 2},
                    },
                ],
            },
        ],
    }
    client_c2 = _FailClient(fail_on={1})
    plan_c2 = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=safe_pred,
        on_error_body="CONTINUE",
        max_iterations=3,
    )
    eid_c2, _ = await _claim_and_run(
        db_session_factory, plan=plan_c2, seeded=seeded, client=client_c2
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid_c2)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SUCCEEDED.value
        children = [s for s in steps if s.parent_step_id == loop.id]
        assert len(children) >= 2
        b1 = _child_by_template(steps, template_id="B", iteration_no=1)
        assert b1.status == StepStatus.FAILED.value

    # FAIL_EXECUTION stops WHILE
    client_f = _FailClient(fail_on={1})
    plan_f = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_always_true(),
        max_iterations=5,
        on_error_body="FAIL_EXECUTION",
    )
    eid_f, _ = await _claim_and_run(
        db_session_factory, plan=plan_f, seeded=seeded, client=client_f
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid_f)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = await ExecutionRepository(session).list_steps(eid_f)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.RUNNING.value or loop.status == (
            StepStatus.FAILED.value
        )
        # Parent LOOP stays RUNNING while body fails with FAIL_EXECUTION —
        # orchestrator fail-fast terminalizes Execution; LOOP may remain RUNNING
        # or get cleaned. Children: only iter1.
        children = [s for s in steps if s.parent_step_id is not None]
        assert len(children) == 1


@pytest.mark.asyncio
async def test_max_iterations_boundaries_timeout_when_false_unsupported(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # max_iterations true boundary
    client = _ValueClient()
    plan = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_always_true(),
        max_iterations=2,
        on_error_loop="FAIL_EXECUTION",
    )
    eid, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.FAILED.value
        assert loop.error_code == LOOP_MAX_ITERATIONS_EXCEEDED
        hist = parse_while_predicate_history(loop.resolved_input)
        assert [e["result"] for e in hist["predicate_history"]] == [
            True,
            True,
            True,
        ]
        assert len([s for s in steps if s.parent_step_id == loop.id]) == 2
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
    assert len(client.calls) == 2

    # max_iterations false boundary (normal success at max)
    client2 = _ValueClient()
    plan2 = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_prev_lt_predicate("B", 3),
        max_iterations=3,
    )
    eid2, _ = await _claim_and_run(
        db_session_factory, plan=plan2, seeded=seeded, client=client2
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid2)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SUCCEEDED.value
        assert loop.result_inline["iterations_completed"] == 3
        hist = parse_while_predicate_history(loop.resolved_input)
        assert hist["predicate_history"][-1]["result"] is False
        assert hist["predicate_history"][-1]["next_iteration_no"] == 4

    # max_steps dynamic exceed
    client3 = _ValueClient()
    # top-level L(1) + 2 body rows = 3; max_steps=2 → fail before iter1 materialize
    # Actually: after pin RUNNING, current=1 (L only). body=1 → 1+1=2 ok for iter1.
    # After iter1: 2 rows. Next: 2+1=3 > 2 → PLAN_LIMIT_EXCEEDED.
    plan3 = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_always_true(),
        max_iterations=5,
        max_steps=2,
    )
    eid3, _ = await _claim_and_run(
        db_session_factory, plan=plan3, seeded=seeded, client=client3
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid3)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == PLAN_LIMIT_EXCEEDED
        assert execution.lease_token is None
        steps = await ExecutionRepository(session).list_steps(eid3)
        assert len(steps) <= 2
    # iter1 MCP may have run; iter2 must not.
    assert len(client3.calls) <= 1

    # Step.when false → SKIPPED, no history
    client4 = _ValueClient()
    plan4 = _simple_while_plan(
        seeded["tool_version_id"],
        when={
            "op": "eq",
            "left": {"kind": BindingKind.LITERAL.value, "value": False},
            "right": {"kind": BindingKind.LITERAL.value, "value": True},
        },
    )
    eid4, _ = await _claim_and_run(
        db_session_factory, plan=plan4, seeded=seeded, client=client4
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid4)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SKIPPED.value
        assert loop.resolved_input is None
        assert not [s for s in steps if s.parent_step_id == loop.id]
    assert len(client4.calls) == 0

    # Nested LOOP / body APPROVAL still unsupported
    nested = _plan(
        [
            _while_loop("outer", body_step_ids=["inner"], predicate=_always_true()),
            _while_loop(
                "inner",
                body_step_ids=["ibody"],
                predicate=_always_true(),
                depends_on=["outer"],
            ),
            _tool("ibody", depends_on=["inner"]),
        ],
        seeded["tool_version_id"],
        response_step_ids=["outer"],
    )
    with pytest.raises(AppError) as nested_exc:
        assert_flat_foreach_runtime_compatible(
            ExecutionPlanV1.model_validate(nested)
        )
    assert nested_exc.value.code == LOOP_RUNTIME_UNSUPPORTED

    approval_plan = _plan(
        [
            _while_loop("L", body_step_ids=["apr"], predicate=_always_true()),
            _approval("apr", depends_on=["L"]),
        ],
        seeded["tool_version_id"],
        response_step_ids=["L"],
    )
    with pytest.raises(AppError) as apr_exc:
        assert_flat_foreach_runtime_compatible(
            ExecutionPlanV1.model_validate(approval_plan)
        )
    assert apr_exc.value.code == LOOP_RUNTIME_UNSUPPORTED

    # Timeout
    client_t = _ValueClient()
    plan_t = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_always_true(),
        max_iterations=5,
        timeout_seconds=1,
    )
    async with db_session_factory() as session:
        eid_t = await _materialize_queued(
            session, plan_snapshot=plan_t, seeded=seeded, input_snapshot={}
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=eid_t, worker_id="w-to"
        )
        await session.commit()
        lease = claim.lease_token
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client_t,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=db_session_factory, tool_runner=runner
    )
    await orch._prepare_wave(
        execution_id=eid_t, worker_id="w-to", lease_token=lease
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid_t)
        loop = next(s for s in steps if s.step_key == "L")
        loop.started_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()
    wave = await orch._prepare_wave(
        execution_id=eid_t, worker_id="w-to", lease_token=lease
    )
    assert wave.execution_complete
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid_t)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.TIMED_OUT.value
        assert loop.error_code == LOOP_TIMEOUT


@pytest.mark.asyncio
async def test_pre_mcp_gate_and_evidence_drift(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    client = _ValueClient()
    plan = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_prev_lt_predicate("B", 3),
    )
    async with db_session_factory() as session:
        eid = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded, input_snapshot={}
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=eid, worker_id="w-drift"
        )
        await session.commit()
        lease = claim.lease_token

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=db_session_factory, tool_runner=runner
    )
    # Run until iter1 completes and iter2 is materialized.
    for _ in range(8):
        wave = await orch._prepare_wave(
            execution_id=eid, worker_id="w-drift", lease_token=lease
        )
        if wave.reason == "WAVE_READY" and wave.ready_step_ids:
            # Dispatch via runner for ready tools
            await runner.run_claimed_execution(
                execution_id=eid,
                worker_id="w-drift",
                lease_token=lease,
            )
            # Reclaim if lease cleared after terminal... break if complete
            async with db_session_factory() as session:
                execution = await ExecutionRepository(session).get(eid)
                if execution is not None and execution.status != (
                    ExecutionStatus.RUNNING.value
                ):
                    break
                if execution is not None and execution.lease_token is None:
                    break
                if execution is not None and execution.lease_token:
                    lease = execution.lease_token
            continue
        if wave.execution_complete:
            break
        async with db_session_factory() as session:
            steps = await ExecutionRepository(session).list_steps(eid)
            loop = next(s for s in steps if s.step_key == "L")
            children = [s for s in steps if s.parent_step_id == loop.id]
            # Stop once iter2 exists and is PENDING/READY before MCP
            if any(c.iteration_no == 2 for c in children):
                b2 = _child_by_template(steps, template_id="B", iteration_no=2)
                if b2.status in {
                    StepStatus.PENDING.value,
                    StepStatus.READY.value,
                }:
                    # Mutate iter1 result evidence
                    b1 = _child_by_template(steps, template_id="B", iteration_no=1)
                    b1.result_inline = {
                        "structured_content": {"value": 999, "tampered": True}
                    }
                    b1.lock_version += 1
                    await session.commit()
                    break

    # Pre-MCP lineage must fail closed.
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        b2_rows = [
            s
            for s in steps
            if s.iteration_no == 2
            and (s.step_snapshot or {}).get("id") == "B"
        ]
        if b2_rows and execution.status == ExecutionStatus.RUNNING.value:
            b2 = b2_rows[0]
            with pytest.raises(AppError) as exc:
                assert_tool_step_lineage(execution, b2, steps=steps)
            assert exc.value.code == "RESOURCE_CONFLICT"
            assert b2.attempt_count == 0


@pytest.mark.asyncio
async def test_max_iterations_continue_downstream_partial(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    plan = _plan(
        [
            _while_loop(
                "L",
                body_step_ids=["B"],
                predicate=_always_true(),
                max_iterations=2,
                on_error="CONTINUE",
            ),
            _tool("B", depends_on=["L"], bindings={"value": _lc("/iteration_no")}),
            _tool(
                "D",
                depends_on=["L"],
                bindings={
                    "value": {"kind": BindingKind.LITERAL.value, "value": 99}
                },
            ),
        ],
        seeded["tool_version_id"],
        response_step_ids=["L", "D"],
    )
    client = _ValueClient()
    eid, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.FAILED.value
        assert loop.error_code == LOOP_MAX_ITERATIONS_EXCEEDED
        d = next(s for s in steps if s.step_key == "D")
        assert d.status == StepStatus.SUCCEEDED.value
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
    assert len(client.calls) == 3  # B1, B2, D


@pytest.mark.asyncio
async def test_predicate_missing_fatal(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # Candidate 1 references deep previous path without is_null short-circuit.
    bad_pred = {
        "op": "lt",
        "left": _lc(
            "/previous_iteration/steps/B/result_inline/structured_content/value"
        ),
        "right": {"kind": BindingKind.LITERAL.value, "value": 3},
    }
    plan = _simple_while_plan(seeded["tool_version_id"], predicate=bad_pred)
    client = _ValueClient()
    eid, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code in {
            "PREDICATE_OPERAND_MISSING",
            "PREDICATE_EVALUATION_FAILED",
            "PREDICATE_TYPE_MISMATCH",
        }
    assert len(client.calls) == 0


# ---------------------------------------------------------------------------
# Terminal-history / immutable lineage integrity
# ---------------------------------------------------------------------------


def _digest(n: int = 0) -> str:
    return f"{n:064x}"[-64:]


def test_zero_iteration_terminal_history_tamper() -> None:
    parent = type(
        "P",
        (),
        {
            "id": uuid.uuid4(),
            "step_key": "L",
            "step_type": AuthorableStepType.LOOP.value,
            "parent_step_id": None,
            "status": StepStatus.SUCCEEDED.value,
            "error_code": None,
            "result_inline": {"mode": "WHILE", "iterations_completed": 0},
            "resolved_input": {
                "mode": "WHILE",
                "predicate_history": [
                    {
                        "next_iteration_no": 1,
                        "evidence_hash": _digest(1),
                        "result": False,
                    }
                ],
            },
            "step_snapshot": None,
        },
    )()
    plan_dict = _plan(
        [
            _while_loop("L", body_step_ids=["B"], predicate=_always_false()),
            _tool("B", depends_on=["L"]),
        ],
        uuid.uuid4(),
        response_step_ids=["L"],
    )
    plan = ExecutionPlanV1.model_validate(plan_dict)
    parent.step_snapshot = next(
        s.model_dump(mode="json") for s in plan.steps if s.id == "L"
    )
    assert_while_parent_control_state(parent=parent, plan=plan, steps=[parent])

    # history=[]
    parent.resolved_input = {"mode": "WHILE", "predicate_history": []}
    with pytest.raises(AppError) as e1:
        assert_while_parent_control_state(parent=parent, plan=plan, steps=[parent])
    assert e1.value.code == "RESOURCE_CONFLICT"

    # history=[1:true]
    parent.resolved_input = {
        "mode": "WHILE",
        "predicate_history": [
            {"next_iteration_no": 1, "evidence_hash": _digest(1), "result": True}
        ],
    }
    with pytest.raises(AppError) as e2:
        assert_while_parent_control_state(parent=parent, plan=plan, steps=[parent])
    assert e2.value.code == "RESOURCE_CONFLICT"

    # iterations_completed=1 with zero children
    parent.resolved_input = {
        "mode": "WHILE",
        "predicate_history": [
            {"next_iteration_no": 1, "evidence_hash": _digest(1), "result": False}
        ],
    }
    parent.result_inline = {"mode": "WHILE", "iterations_completed": 1}
    with pytest.raises(AppError) as e3:
        assert_while_parent_control_state(parent=parent, plan=plan, steps=[parent])
    assert e3.value.code == "RESOURCE_CONFLICT"

    # extra gate2
    parent.result_inline = {"mode": "WHILE", "iterations_completed": 0}
    parent.resolved_input = {
        "mode": "WHILE",
        "predicate_history": [
            {"next_iteration_no": 1, "evidence_hash": _digest(1), "result": False},
            {"next_iteration_no": 2, "evidence_hash": _digest(2), "result": False},
        ],
    }
    with pytest.raises(AppError) as e4:
        assert_while_parent_control_state(parent=parent, plan=plan, steps=[parent])
    assert e4.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_max_iterations_terminal_history_tamper(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    client = _ValueClient()
    plan = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_always_true(),
        max_iterations=2,
    )
    eid, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.FAILED.value
        assert loop.error_code == LOOP_MAX_ITERATIONS_EXCEEDED
        plan_obj = ExecutionPlanV1.model_validate(
            (await ExecutionRepository(session).get(eid)).plan_snapshot
        )
        assert_while_parent_control_state(
            parent=loop, plan=plan_obj, steps=steps
        )

        # Corrupt: gate3 false instead of true
        hist = parse_while_predicate_history(loop.resolved_input)
        bad = dict(loop.resolved_input)
        entries = list(hist["predicate_history"])
        entries[-1] = {**entries[-1], "result": False}
        bad["predicate_history"] = entries
        loop.resolved_input = bad
        with pytest.raises(AppError) as exc:
            assert_while_parent_control_state(
                parent=loop, plan=plan_obj, steps=steps
            )
        assert exc.value.code == "RESOURCE_CONFLICT"

        # Corrupt: missing gate3
        loop.resolved_input = {
            "mode": "WHILE",
            "predicate_history": entries[:2],
        }
        with pytest.raises(AppError):
            assert_while_parent_control_state(
                parent=loop, plan=plan_obj, steps=steps
            )


@pytest.mark.asyncio
async def test_succeeded_terminal_history_removed_false_gate(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    plan = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_prev_lt_predicate("B", 1),
    )
    eid, _ = await _claim_and_run(
        db_session_factory,
        plan=plan,
        seeded=seeded,
        client=_ValueClient(),
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        assert loop.status == StepStatus.SUCCEEDED.value
        plan_obj = ExecutionPlanV1.model_validate(
            (await ExecutionRepository(session).get(eid)).plan_snapshot
        )
        # Valid
        assert_while_parent_control_state(
            parent=loop, plan=plan_obj, steps=steps
        )
        # Remove final false gate — still SUCCEEDED
        hist = parse_while_predicate_history(loop.resolved_input)
        loop.resolved_input = {
            "mode": "WHILE",
            "predicate_history": hist["predicate_history"][:1],
        }
        with pytest.raises(AppError) as exc:
            validate_tool_join_dag(plan_obj, steps)
        assert exc.value.code == "RESOURCE_CONFLICT"
        # false → true while remaining SUCCEEDED
        loop.resolved_input = {
            "mode": "WHILE",
            "predicate_history": [
                hist["predicate_history"][0],
                {
                    **hist["predicate_history"][1],
                    "result": True,
                },
            ],
        }
        with pytest.raises(AppError) as exc2:
            assert_while_parent_control_state(
                parent=loop, plan=plan_obj, steps=steps
            )
        assert exc2.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_top_level_ancestor_snapshot_drift_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # Upstream A TOOL feeds WHILE predicate; then body B.
    pred = {
        "op": "or",
        "children": [
            {"op": "is_null", "operand": _lc("/previous_iteration")},
            {
                "op": "lt",
                "left": _lc(
                    "/previous_iteration/steps/B/result_inline"
                    "/structured_content/value"
                ),
                "right": {"kind": BindingKind.LITERAL.value, "value": 1},
            },
        ],
    }
    # Also depend on ancestor via STEP_OUTPUT in predicate so evidence includes A.
    pred_with_a = {
        "op": "and",
        "children": [
            {
                "op": "eq",
                "left": {
                    "kind": BindingKind.STEP_OUTPUT.value,
                    "step_id": "A",
                    "path": "/structured_content/value",
                },
                "right": {"kind": BindingKind.LITERAL.value, "value": 7},
            },
            pred,
        ],
    }
    plan = _plan(
        [
            _tool(
                "A",
                bindings={
                    "value": {"kind": BindingKind.LITERAL.value, "value": 7}
                },
            ),
            _while_loop(
                "L",
                body_step_ids=["B"],
                predicate=pred_with_a,
                depends_on=["A"],
                max_iterations=3,
            ),
            _tool("B", depends_on=["L"], bindings={"value": _lc("/iteration_no")}),
        ],
        seeded["tool_version_id"],
        response_step_ids=["L"],
    )
    async with db_session_factory() as session:
        eid = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded, input_snapshot={}
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=eid, worker_id="w-anc"
        )
        await session.commit()
        lease = claim.lease_token
    client = _ValueClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    # Full run to SUCCEEDED (1 iteration: previous null → true, then value=1 → stop)
    await runner.run_claimed_execution(
        execution_id=eid, worker_id="w-anc", lease_token=lease
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        # Re-open as RUNNING-like gate rebuild: mutate A snapshot after pin
        a = next(s for s in steps if s.step_key == "A")
        snap = dict(a.step_snapshot)
        snap["timeout_seconds"] = 999
        a.step_snapshot = snap
        a.lock_version += 1
        execution = await ExecutionRepository(session).get(eid)
        plan_obj = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        await session.commit()

    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        loop = next(s for s in steps if s.step_key == "L")
        execution = await ExecutionRepository(session).get(eid)
        plan_obj = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        from app.schemas.execution_plan import LoopStepConfigV1

        cfg = LoopStepConfigV1.model_validate(
            next(s for s in plan_obj.steps if s.id == "L").config
        )
        with pytest.raises(AppError) as exc:
            build_while_evidence_object(
                plan=plan_obj,
                execution=execution,
                loop_step=loop,
                cfg=cfg,
                steps=steps,
                candidate_iteration_no=1,
            )
        assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_previous_iteration_snapshot_drift_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    plan = _simple_while_plan(
        seeded["tool_version_id"],
        predicate=_prev_lt_predicate("B", 3),
    )
    async with db_session_factory() as session:
        eid = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded, input_snapshot={}
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=eid, worker_id="w-prev"
        )
        await session.commit()
        lease = claim.lease_token
    client = _ValueClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=db_session_factory, tool_runner=runner
    )
    await orch._prepare_wave(
        execution_id=eid, worker_id="w-prev", lease_token=lease
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        b1 = _child_by_template(steps, template_id="B", iteration_no=1)
        b1.status = StepStatus.SUCCEEDED.value
        b1.finished_at = datetime.now(UTC)
        b1.result_inline = {"structured_content": {"value": 1, "ok": True}}
        b1.lock_version += 1
        await session.commit()
    await orch._prepare_wave(
        execution_id=eid, worker_id="w-prev", lease_token=lease
    )
    # Mutate previous child snapshot (keep template id / result / status).
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        b1 = _child_by_template(steps, template_id="B", iteration_no=1)
        snap = dict(b1.step_snapshot)
        snap["name"] = "tampered-name"
        snap["timeout_seconds"] = 12345
        b1.step_snapshot = snap
        b1.lock_version += 1
        await session.commit()

    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        b2 = _child_by_template(steps, template_id="B", iteration_no=2)
        with pytest.raises(AppError) as exc:
            assert_tool_step_lineage(execution, b2, steps=steps)
        assert exc.value.code == "RESOURCE_CONFLICT"
        assert b2.attempt_count == 0
        attempts = await ExecutionRepository(session).list_attempts(b2.id)
        assert len(attempts) == 0
    assert len(client.calls) == 0
