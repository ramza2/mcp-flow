"""Unit tests for ExecutionPlanMaterializer (multi-step CREATED/PENDING only)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    JoinPolicy,
    LoopMode,
    StepStatus,
)
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.models.execution import Execution, ExecutionStep
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.user import UserRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    compute_plan_hash,
    default_plan_limits,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_creation import (
    _create,
    _idem_key,
    _install_no_side_effects,
    _seed_ready,
)

_AV = uuid.uuid4()
_AP = uuid.uuid4()


def _base_plan(
    *,
    steps: list[dict[str, Any]],
    tool_version_id: uuid.UUID,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Rewrite TOOL configs to use the seeded ToolVersion id.
    rewritten: list[dict[str, Any]] = []
    for step in steps:
        s = dict(step)
        if s.get("type") == AuthorableStepType.TOOL.value:
            cfg = dict(s.get("config") or {})
            cfg["tool_version_id"] = str(tool_version_id)
            s["config"] = cfg
        rewritten.append(s)
    response_ids = [s["id"] for s in rewritten if s.get("type") == "TOOL"] or [
        rewritten[-1]["id"]
    ]
    lim = default_plan_limits().model_dump(mode="json")
    if limits:
        lim.update(limits)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "materializer fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": lim,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_ids,
        },
    }


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
            "bindings": bindings
            or {"location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}},
        },
    }


def _join(sid: str, *, depends_on: list[str]) -> dict[str, Any]:
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


def _condition(sid: str, *, depends_on: list[str], upstream: str) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.CONDITION.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {
            "predicate": {
                "op": "eq",
                "left": {
                    "kind": BindingKind.STEP_OUTPUT.value,
                    "step_id": upstream,
                    "path": "/ok",
                },
                "right": {"kind": BindingKind.LITERAL.value, "value": True},
            }
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


def _loop(sid: str, *, body_step_ids: list[str]) -> dict[str, Any]:
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
            "max_iterations": 5,
            "collection": {
                "kind": BindingKind.PLAN_INPUT.value,
                "path": "/items",
            },
            "body_step_ids": body_step_ids,
        },
    }


async def _seed_requester_and_tool(
    session: AsyncSession,
) -> dict[str, Any]:
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="Materializer",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name="Mat Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status="ACTIVE",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name="mat_tool",
        display_name="mat_tool",
        tags=[],
        status="ACTIVE",
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status="VALID",
        remote_description="mat",
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    await session.flush()
    return {"requester_id": user.id, "tool_version_id": version.id}


async def _materialize(
    session: AsyncSession,
    *,
    plan_snapshot: dict[str, Any],
    requester_id: uuid.UUID,
    plan_hash: str | None = None,
) -> Any:
    digest = plan_hash if plan_hash is not None else compute_plan_hash(plan_snapshot)
    return await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=requester_id,
            plan_snapshot=plan_snapshot,
            plan_hash=digest,
            input_snapshot={},
            policy_snapshot={"risk_class": "READ_ONLY"},
            requested_at=datetime.now(UTC),
            trace_id="trace-mat",
        )
    )


async def _count_executions(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(Execution))).scalar_one()
    )


async def _count_steps(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(select(func.count()).select_from(ExecutionStep))
        ).scalar_one()
    )


async def _count_outbox(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(OutboxEvent))).scalar_one()
    )


async def _count_approvals(session: AsyncSession) -> int:
    # Avoid importing ApprovalRequest (registers metadata table and breaks
    # execution-creation unit assertions that check table absence).
    result = await session.execute(
        text(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='table' AND name='approval_requests'"
        )
    )
    if int(result.scalar_one()) == 0:
        return 0
    return int(
        (await session.execute(text("SELECT count(*) FROM approval_requests"))).scalar_one()
    )


@pytest.mark.asyncio
async def test_sequential_two_tool_pending_rows(db_session: AsyncSession) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[_tool("a"), _tool("b", depends_on=["a"])],
        tool_version_id=seeded["tool_version_id"],
    )
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()

    assert result.execution.status == ExecutionStatus.CREATED.value
    assert result.execution.plan_hash == compute_plan_hash(plan)
    assert result.execution.plan_snapshot == plan
    assert result.execution.input_snapshot == {}
    assert len(result.steps) == 2
    steps = await ExecutionRepository(db_session).list_steps(result.execution.id)
    assert [s.step_key for s in steps] == ["a", "b"]
    assert all(s.status == StepStatus.PENDING.value for s in steps)
    assert all(s.ready_at is None and s.started_at is None for s in steps)
    assert all(s.attempt_count == 0 for s in steps)
    assert all(s.parent_step_id is None for s in steps)
    assert steps[1].step_snapshot["depends_on"] == ["a"]
    assert await _count_outbox(db_session) == 0
    assert await _count_approvals(db_session) == 0


@pytest.mark.asyncio
async def test_fan_out_fan_in_exact_n_pending(db_session: AsyncSession) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan_steps = [
        _tool("root"),
        _tool("left", depends_on=["root"]),
        _tool("right", depends_on=["root"]),
        _join("join1", depends_on=["left", "right"]),
        _tool("final", depends_on=["join1"]),
    ]
    plan = _base_plan(steps=plan_steps, tool_version_id=seeded["tool_version_id"])
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()
    steps = await ExecutionRepository(db_session).list_steps(result.execution.id)
    assert len(steps) == 5
    assert all(s.status == StepStatus.PENDING.value for s in steps)
    assert {s.step_key for s in steps} == {"root", "left", "right", "join1", "final"}


@pytest.mark.asyncio
async def test_sequence_hint_follows_plan_array_order(db_session: AsyncSession) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[
            _tool("z_last_in_name"),
            _tool("a_second", depends_on=["z_last_in_name"]),
            _tool("m_third", depends_on=["a_second"]),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()
    steps = await ExecutionRepository(db_session).list_steps(result.execution.id)
    assert [(s.step_key, s.sequence_hint) for s in steps] == [
        ("z_last_in_name", 0),
        ("a_second", 1),
        ("m_third", 2),
    ]


@pytest.mark.asyncio
async def test_tool_projects_mcp_tool_version_id(db_session: AsyncSession) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[_tool("only")],
        tool_version_id=seeded["tool_version_id"],
    )
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()
    assert result.steps[0].mcp_tool_version_id == seeded["tool_version_id"]
    assert result.steps[0].step_type == AuthorableStepType.TOOL.value


@pytest.mark.asyncio
async def test_non_tool_types_project_null_tool_version(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[
            _tool("t1"),
            _condition("c1", depends_on=["t1"], upstream="t1"),
            _approval("apr1", depends_on=["c1"]),
            _join("j1", depends_on=["apr1"]),
            _loop("loop1", body_step_ids=["body"]),
            _tool("body", depends_on=["loop1"]),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()
    by_key = {s.step_key: s for s in result.steps}
    assert by_key["t1"].mcp_tool_version_id == seeded["tool_version_id"]
    assert by_key["body"].mcp_tool_version_id == seeded["tool_version_id"]
    for key in ("c1", "apr1", "j1", "loop1"):
        assert by_key[key].mcp_tool_version_id is None
        assert by_key[key].status == StepStatus.PENDING.value


@pytest.mark.asyncio
async def test_exact_step_snapshot_preservation(db_session: AsyncSession) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[
            _tool("a"),
            _tool(
                "b",
                depends_on=["a"],
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/result",
                    }
                },
            ),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()
    for index, step in enumerate(result.steps):
        assert step.step_snapshot == plan["steps"][index]
        assert step.parent_step_id is None
        assert "depends_on" in step.step_snapshot


@pytest.mark.asyncio
async def test_hash_mismatch_creates_zero_rows(db_session: AsyncSession) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[_tool("a")],
        tool_version_id=seeded["tool_version_id"],
    )
    before_e = await _count_executions(db_session)
    before_s = await _count_steps(db_session)
    with pytest.raises(AppError) as exc:
        await _materialize(
            db_session,
            plan_snapshot=plan,
            requester_id=seeded["requester_id"],
            plan_hash="0" * 64,
        )
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"
    await db_session.rollback()
    assert await _count_executions(db_session) == before_e
    assert await _count_steps(db_session) == before_s


@pytest.mark.asyncio
async def test_malformed_step_config_creates_zero_rows(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[_tool("a"), _join("j", depends_on=["a"])],
        tool_version_id=seeded["tool_version_id"],
    )
    plan["steps"][1]["config"] = {"policy": "NOT_A_POLICY"}
    before_e = await _count_executions(db_session)
    before_s = await _count_steps(db_session)
    with pytest.raises(AppError) as exc:
        await _materialize(
            db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
        )
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"
    await db_session.rollback()
    assert await _count_executions(db_session) == before_e
    assert await _count_steps(db_session) == before_s


@pytest.mark.asyncio
async def test_mid_materialization_failure_full_rollback(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[_tool("a"), _tool("b", depends_on=["a"]), _tool("c", depends_on=["b"])],
        tool_version_id=seeded["tool_version_id"],
    )
    before_e = await _count_executions(db_session)
    before_s = await _count_steps(db_session)
    before_o = await _count_outbox(db_session)

    real_create_step = ExecutionRepository.create_step
    calls = {"n": 0}

    async def _failing_create_step(self: ExecutionRepository, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated mid-materialization failure")
        return await real_create_step(self, **kwargs)

    monkeypatch.setattr(ExecutionRepository, "create_step", _failing_create_step)

    with pytest.raises(RuntimeError, match="simulated mid-materialization failure"):
        await _materialize(
            db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
        )
    await db_session.rollback()

    assert await _count_executions(db_session) == before_e
    assert await _count_steps(db_session) == before_s
    assert await _count_outbox(db_session) == before_o
    assert await _count_approvals(db_session) == 0


@pytest.mark.asyncio
async def test_agent_request_single_tool_regression(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    outcome = await _create(db_session, seeded, idempotency_key=_idem_key())
    assert outcome.result.status == ExecutionStatus.CREATED.value
    assert outcome.result.step_count == 1
    steps = await ExecutionRepository(db_session).list_steps(outcome.result.id)
    assert len(steps) == 1
    assert steps[0].status == StepStatus.PENDING.value
    assert steps[0].step_type == AuthorableStepType.TOOL.value
    assert steps[0].parent_step_id is None
    assert await _count_outbox(db_session) == 0
    assert await _count_approvals(db_session) == 0


@pytest.mark.asyncio
async def test_no_secret_resolver_or_mcp_on_materialize(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_requester_and_tool(db_session)
    boom = AsyncMock(side_effect=AssertionError("must not call MCP"))
    monkeypatch.setattr("app.mcp.client.MCPHttpClient.request", boom, raising=False)
    plan = _base_plan(
        steps=[_tool("a"), _tool("b", depends_on=["a"])],
        tool_version_id=seeded["tool_version_id"],
    )
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()
    assert len(result.steps) == 2
    boom.assert_not_awaited()


# --- StaticComplexPlanValidator revalidation (fail before first DB insert) ---


async def _assert_static_reject_zero_rows(
    session: AsyncSession,
    *,
    plan: dict[str, Any],
    requester_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> AppError:
    before_e = await _count_executions(session)
    before_s = await _count_steps(session)
    before_o = await _count_outbox(session)
    insert_calls = {"n": 0}

    if monkeypatch is not None:
        real_create = ExecutionRepository.create_execution

        async def _spy_create(self: ExecutionRepository, **kwargs: Any) -> Any:
            insert_calls["n"] += 1
            return await real_create(self, **kwargs)

        monkeypatch.setattr(ExecutionRepository, "create_execution", _spy_create)

    with pytest.raises(AppError) as exc:
        await _materialize(session, plan_snapshot=plan, requester_id=requester_id)
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"
    assert "static complex-plan validation failed" in exc.value.message
    await session.rollback()
    assert await _count_executions(session) == before_e
    assert await _count_steps(session) == before_s
    assert await _count_outbox(session) == before_o
    assert insert_calls["n"] == 0
    return exc.value


@pytest.mark.asyncio
async def test_static_reject_missing_dependency_zero_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[_tool("a", depends_on=["missing"])],
        tool_version_id=seeded["tool_version_id"],
    )
    err = await _assert_static_reject_zero_rows(
        db_session,
        plan=plan,
        requester_id=seeded["requester_id"],
        monkeypatch=monkeypatch,
    )
    assert any(d.get("code") == "PLAN_DEPENDENCY_MISSING" for d in err.details)


@pytest.mark.asyncio
async def test_static_reject_cycle_zero_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[
            _tool("a", depends_on=["b"]),
            _tool("b", depends_on=["a"]),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    err = await _assert_static_reject_zero_rows(
        db_session,
        plan=plan,
        requester_id=seeded["requester_id"],
        monkeypatch=monkeypatch,
    )
    assert any(d.get("code") == "PLAN_CYCLE_DETECTED" for d in err.details)


@pytest.mark.asyncio
async def test_static_reject_forward_sibling_step_output_zero_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    # forward/downstream reference
    forward = _base_plan(
        steps=[
            _tool(
                "a",
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "b",
                        "path": "/result",
                    }
                },
            ),
            _tool("b", depends_on=["a"]),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    err = await _assert_static_reject_zero_rows(
        db_session,
        plan=forward,
        requester_id=seeded["requester_id"],
        monkeypatch=monkeypatch,
    )
    assert any(d.get("code") == "PLAN_BINDING_INVALID" for d in err.details)

    # unrelated sibling
    sibling = _base_plan(
        steps=[
            _tool("root"),
            _tool("left", depends_on=["root"]),
            _tool(
                "right",
                depends_on=["root"],
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "left",
                        "path": "/result",
                    }
                },
            ),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    err2 = await _assert_static_reject_zero_rows(
        db_session,
        plan=sibling,
        requester_id=seeded["requester_id"],
        monkeypatch=monkeypatch,
    )
    assert any(d.get("code") == "PLAN_BINDING_INVALID" for d in err2.details)


@pytest.mark.asyncio
async def test_static_reject_invalid_loop_body_scope_zero_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[
            _tool("outside"),
            _loop("loop1", body_step_ids=["body"]),
            # body depends on a non-body / non-LOOP step → invalid scope
            _tool("body", depends_on=["outside"]),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    err = await _assert_static_reject_zero_rows(
        db_session,
        plan=plan,
        requester_id=seeded["requester_id"],
        monkeypatch=monkeypatch,
    )
    assert any(
        d.get("code") == "PLAN_SCHEMA_INVALID"
        and d.get("step_id") == "body"
        for d in err.details
    )


@pytest.mark.asyncio
async def test_static_reject_limit_violation_zero_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[
            _tool("a"),
            _tool("b", depends_on=["a"]),
            _tool("c", depends_on=["b"]),
        ],
        tool_version_id=seeded["tool_version_id"],
        limits={"max_steps": 2},
    )
    err = await _assert_static_reject_zero_rows(
        db_session,
        plan=plan,
        requester_id=seeded["requester_id"],
        monkeypatch=monkeypatch,
    )
    assert any(d.get("code") == "PLAN_LIMIT_EXCEEDED" for d in err.details)


@pytest.mark.asyncio
async def test_valid_multistep_still_materializes_after_static_check(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_requester_and_tool(db_session)
    plan = _base_plan(
        steps=[
            _tool("root"),
            _tool("left", depends_on=["root"]),
            _tool("right", depends_on=["root"]),
            _join("join1", depends_on=["left", "right"]),
            _tool("final", depends_on=["join1"]),
        ],
        tool_version_id=seeded["tool_version_id"],
    )
    result = await _materialize(
        db_session, plan_snapshot=plan, requester_id=seeded["requester_id"]
    )
    await db_session.commit()
    assert result.execution.status == ExecutionStatus.CREATED.value
    assert len(result.steps) == 5
    assert all(s.status == StepStatus.PENDING.value for s in result.steps)
