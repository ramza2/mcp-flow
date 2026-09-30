"""PostgreSQL integration tests for multi-step Execution materialization."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    JoinPolicy,
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed(session: AsyncSession) -> dict[str, Any]:
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="PG Mat",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name="PG Mat Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status="ACTIVE",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name="pg_mat",
        display_name="pg_mat",
        tags=[],
        status="ACTIVE",
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status="VALID",
        remote_description="pg mat",
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    await session.flush()
    return {"requester_id": user.id, "tool_version_id": version.id}


def _plan(tool_version_id: uuid.UUID, steps: list[dict[str, Any]]) -> dict[str, Any]:
    rewritten = []
    for step in steps:
        s = dict(step)
        if s.get("type") == AuthorableStepType.TOOL.value:
            cfg = dict(s.get("config") or {})
            cfg["tool_version_id"] = str(tool_version_id)
            s["config"] = cfg
        rewritten.append(s)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "pg materializer",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_multistep_atomic_persist(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed(session)
        plan = _plan(
            seeded["tool_version_id"],
            [
                _tool("root"),
                _tool("left", depends_on=["root"]),
                _tool("right", depends_on=["root"]),
                _join("join1", depends_on=["left", "right"]),
                _tool("final", depends_on=["join1"]),
            ],
        )
        result = await ExecutionPlanMaterializer(session).materialize(
            ExecutionMaterializeParams(
                source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
                trigger_type="TEST",
                requester_id=seeded["requester_id"],
                plan_snapshot=plan,
                plan_hash=compute_plan_hash(plan),
                input_snapshot={},
                policy_snapshot={"risk_class": "READ_ONLY"},
                requested_at=datetime.now(UTC),
            )
        )
        execution_id = result.execution.id
        await session.commit()

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CREATED.value
        assert execution.plan_hash == compute_plan_hash(execution.plan_snapshot)
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 5
        assert all(s.status == StepStatus.PENDING.value for s in steps)
        assert all(s.parent_step_id is None for s in steps)
        assert [s.sequence_hint for s in steps] == [0, 1, 2, 3, 4]
        join = next(s for s in steps if s.step_key == "join1")
        assert join.mcp_tool_version_id is None
        assert join.step_snapshot["depends_on"] == ["left", "right"]
        outbox_count = (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == execution_id)
            )
        ).scalar_one()
        assert int(outbox_count) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_hash_mismatch_zero_rows(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed(session)
        plan = _plan(seeded["tool_version_id"], [_tool("a"), _tool("b", depends_on=["a"])])
        before_e = (
            await session.execute(select(func.count()).select_from(Execution))
        ).scalar_one()
        before_s = (
            await session.execute(select(func.count()).select_from(ExecutionStep))
        ).scalar_one()
        with pytest.raises(AppError) as exc:
            await ExecutionPlanMaterializer(session).materialize(
                ExecutionMaterializeParams(
                    source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
                    trigger_type="TEST",
                    requester_id=seeded["requester_id"],
                    plan_snapshot=plan,
                    plan_hash="f" * 64,
                    input_snapshot={},
                    policy_snapshot={},
                    requested_at=datetime.now(UTC),
                )
            )
        assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"
        await session.rollback()
        after_e = (
            await session.execute(select(func.count()).select_from(Execution))
        ).scalar_one()
        after_s = (
            await session.execute(select(func.count()).select_from(ExecutionStep))
        ).scalar_one()
        assert after_e == before_e
        assert after_s == before_s


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_mid_step_failure_rolls_back(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed(session)
        plan = _plan(
            seeded["tool_version_id"],
            [_tool("a"), _tool("b", depends_on=["a"]), _tool("c", depends_on=["b"])],
        )
        before_e = (
            await session.execute(select(func.count()).select_from(Execution))
        ).scalar_one()
        before_s = (
            await session.execute(select(func.count()).select_from(ExecutionStep))
        ).scalar_one()

        real = ExecutionRepository.create_step
        n = {"c": 0}

        async def _boom(self: ExecutionRepository, **kwargs: Any) -> Any:
            n["c"] += 1
            if n["c"] >= 2:
                raise RuntimeError("pg mid-fail")
            return await real(self, **kwargs)

        monkeypatch.setattr(ExecutionRepository, "create_step", _boom)
        with pytest.raises(RuntimeError, match="pg mid-fail"):
            await ExecutionPlanMaterializer(session).materialize(
                ExecutionMaterializeParams(
                    source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
                    trigger_type="TEST",
                    requester_id=seeded["requester_id"],
                    plan_snapshot=plan,
                    plan_hash=compute_plan_hash(plan),
                    input_snapshot={},
                    policy_snapshot={},
                    requested_at=datetime.now(UTC),
                )
            )
        await session.rollback()
        after_e = (
            await session.execute(select(func.count()).select_from(Execution))
        ).scalar_one()
        after_s = (
            await session.execute(select(func.count()).select_from(ExecutionStep))
        ).scalar_one()
        assert after_e == before_e
        assert after_s == before_s
        # Sanity: no orphaned rows via raw SQL either
        orphan = (
            await session.execute(
                text(
                    "SELECT count(*) FROM execution_steps es "
                    "LEFT JOIN executions e ON e.id = es.execution_id "
                    "WHERE e.id IS NULL"
                )
            )
        ).scalar_one()
        assert int(orphan) == 0
