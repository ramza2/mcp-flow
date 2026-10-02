"""PostgreSQL integration tests for Workflow registry foundation."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    JoinPolicy,
    LoopMode,
    WorkflowStatus,
    WorkflowVersionStatus,
    WorkflowVersionValidationStatus,
    WorkflowVisibility,
)
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.user import UserRepository
from app.repositories.workflow import WorkflowRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.repositories.workflow_version_tool_ref import WorkflowVersionToolRefRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    default_plan_limits,
)
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowPlanPut,
    WorkflowVersionCreate,
)
from app.services.workflow import WorkflowService
from app.services.workflow_version import WorkflowVersionService
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AV = uuid.uuid4()


async def _seed_tool_version(session: AsyncSession) -> uuid.UUID:
    server = await MCPServerRepository(session).create(
        code=f"wf-int-{uuid.uuid4().hex[:8]}",
        name="Integration WF Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status="ACTIVE",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=f"tool_{uuid.uuid4().hex[:6]}",
        display_name="int_tool",
        tags=[],
        status="ACTIVE",
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status="VALID",
        remote_description="int",
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    await session.flush()
    return version.id


async def _seed_approval_policy(
    session: AsyncSession, *, status: str = "ACTIVE"
) -> uuid.UUID:
    policy = await ApprovalPolicyRepository(session).create(
        code=f"ap-int-{uuid.uuid4().hex[:8]}",
        name="Integration Approval",
        status=status,
    )
    await session.flush()
    return policy.id


def _base_plan(
    *,
    steps: list[dict[str, Any]],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lim = default_plan_limits().model_dump(mode="json")
    if limits:
        lim.update(limits)
    body_ids: set[str] = set()
    for s in steps:
        if s.get("type") == AuthorableStepType.LOOP.value:
            cfg = s.get("config") or {}
            body_ids.update(cfg.get("body_step_ids") or [])
    response_ids = [
        s["id"]
        for s in steps
        if s.get("type") == "TOOL" and s["id"] not in body_ids
    ] or (
        [s["id"] for s in steps if s["id"] not in body_ids][-1:] if steps else []
    )
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "workflow integration fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": lim,
        "steps": steps,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_ids,
        },
    }


def _tool(
    sid: str,
    *,
    tool_version_id: uuid.UUID,
    depends_on: list[str] | None = None,
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
            "tool_version_id": str(tool_version_id),
            "bindings": {
                "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
            },
        },
    }


def _tool_plan(tool_version_id: uuid.UUID) -> dict[str, Any]:
    return _base_plan(steps=[_tool("step_a", tool_version_id=tool_version_id)])


def _complex_plan(
    *,
    tool_version_id: uuid.UUID,
    approval_policy_id: uuid.UUID,
) -> dict[str, Any]:
    steps = [
        _tool("t1", tool_version_id=tool_version_id),
        {
            "id": "cond1",
            "name": "cond1",
            "type": AuthorableStepType.CONDITION.value,
            "required": True,
            "depends_on": ["t1"],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "predicate": {
                    "op": "eq",
                    "left": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "t1",
                        "path": "/ok",
                    },
                    "right": {"kind": BindingKind.LITERAL.value, "value": True},
                }
            },
        },
        _tool("t2", tool_version_id=tool_version_id, depends_on=["t1"]),
        {
            "id": "join1",
            "name": "join1",
            "type": AuthorableStepType.JOIN.value,
            "required": True,
            "depends_on": ["cond1", "t2"],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {"policy": JoinPolicy.ALL_SUCCESS.value},
        },
        {
            "id": "apr1",
            "name": "apr1",
            "type": AuthorableStepType.APPROVAL.value,
            "required": True,
            "depends_on": ["join1"],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {"approval_policy_id": str(approval_policy_id)},
        },
        {
            "id": "loop1",
            "name": "loop1",
            "type": AuthorableStepType.LOOP.value,
            "required": True,
            "depends_on": ["apr1"],
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
                "body_step_ids": ["body_tool"],
            },
        },
        _tool("body_tool", tool_version_id=tool_version_id, depends_on=["loop1"]),
        _tool("final", tool_version_id=tool_version_id, depends_on=["loop1"]),
    ]
    plan = _base_plan(steps=steps, limits={"max_parallelism": 4})
    plan["completion"]["response_step_ids"] = ["final"]
    return plan


# ---------------------------------------------------------------------------
# A. Alembic + FK
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_alembic_workflow_registry_upgrade_head(integration_database_url: str) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = integration_database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_workflow_version_fk(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="FK Workflow", visibility=WorkflowVisibility.PRIVATE)
        )
        tv = await _seed_tool_version(session)
        version = await WorkflowVersionService(session).create_version(
            workflow.id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv)),
        )
        user = await UserRepository(session).create(
            username=f"u-{uuid.uuid4().hex[:8]}",
            display_name="WF FK",
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            status="ACTIVE",
        )
        await session.commit()
        version_id = version.id
        user_id = user.id

    async with integration_session_factory() as session:
        exec_row = await ExecutionRepository(session).create_execution(
            source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
            trigger_type="MANUAL",
            requester_id=user_id,
            agent_request_id=None,
            agent_version_id=None,
            workflow_version_id=version_id,
            plan_validation_run_id=None,
            status=ExecutionStatus.CREATED.value,
            plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
            plan_snapshot=_tool_plan(uuid.uuid4()),
            plan_hash="a" * 64,
            input_snapshot={},
            policy_snapshot={},
            trace_id="wf-fk",
            requested_at=datetime.now(UTC),
        )
        await session.commit()
        assert exec_row.workflow_version_id == version_id

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await ExecutionRepository(session).create_execution(
                source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
                trigger_type="MANUAL",
                requester_id=user_id,
                agent_request_id=None,
                agent_version_id=None,
                workflow_version_id=uuid.uuid4(),
                plan_validation_run_id=None,
                status=ExecutionStatus.CREATED.value,
                plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
                plan_snapshot={},
                plan_hash="b" * 64,
                input_snapshot={},
                policy_snapshot={},
                trace_id="wf-fk-bad",
                requested_at=datetime.now(UTC),
            )
            await session.commit()
        await session.rollback()


# ---------------------------------------------------------------------------
# B–F. Validate / publish / invalidate / cascade deprecate
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_valid_tool_workflow_validate_and_publish(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        service = WorkflowService(session)
        versions = WorkflowVersionService(session)
        workflow = await service.create(WorkflowCreate(name="Publish Tool WF"))
        tv = await _seed_tool_version(session)
        version = await versions.create_version(
            workflow.id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv)),
        )
        validated = await versions.validate(workflow.id, version.id)
        assert validated.validation_status == WorkflowVersionValidationStatus.VALID
        refs = await WorkflowVersionToolRefRepository(session).list_for_version(
            version.id
        )
        assert len(refs) == 1
        assert refs[0].step_key == "step_a"

        published = await versions.publish(workflow.id, version.id)
        assert published.status == WorkflowVersionStatus.PUBLISHED
        wf = await service.get(workflow.id)
        assert wf.current_version_id == published.id
        assert wf.status == WorkflowStatus.DRAFT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complex_plan_tool_refs_projected_once(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        versions = WorkflowVersionService(session)
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="Complex WF")
        )
        tv = await _seed_tool_version(session)
        policy_id = await _seed_approval_policy(session)
        plan = _complex_plan(tool_version_id=tv, approval_policy_id=policy_id)
        version = await versions.create_version(
            workflow.id, WorkflowVersionCreate(plan_definition=plan)
        )
        validated = await versions.validate(workflow.id, version.id)
        assert validated.validation_status == WorkflowVersionValidationStatus.VALID, (
            validated.validation_report
        )
        refs = await WorkflowVersionToolRefRepository(session).list_for_version(
            version.id
        )
        keys = sorted(r.step_key for r in refs)
        assert keys == ["body_tool", "final", "t1", "t2"]
        assert len(keys) == len(set(keys))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_cyclic_draft_keeps_row_clears_refs(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        versions = WorkflowVersionService(session)
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="Cycle WF")
        )
        tv = await _seed_tool_version(session)
        cycle = _base_plan(
            steps=[
                _tool("a", tool_version_id=tv, depends_on=["b"]),
                _tool("b", tool_version_id=tv, depends_on=["a"]),
            ]
        )
        version = await versions.create_version(
            workflow.id, WorkflowVersionCreate(plan_definition=cycle)
        )
        version_id = version.id
        result = await versions.validate(workflow.id, version.id)
        assert result.validation_status == WorkflowVersionValidationStatus.INVALID
        codes = [e["code"] for e in (result.validation_report or {}).get("errors", [])]
        assert "PLAN_CYCLE_DETECTED" in codes
        assert (
            await WorkflowVersionToolRefRepository(session).list_for_version(version_id)
        ) == []
        still = await WorkflowVersionRepository(session).get(version_id)
        assert still is not None
        assert still.status == WorkflowVersionStatus.DRAFT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_edit_invalidates_after_valid(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        versions = WorkflowVersionService(session)
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="Invalidate WF")
        )
        tv = await _seed_tool_version(session)
        version = await versions.create_version(
            workflow.id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv)),
        )
        await versions.validate(workflow.id, version.id)
        assert (
            await WorkflowVersionToolRefRepository(session).list_for_version(version.id)
        )
        old_hash = version.content_hash

        updated = await versions.put_plan(
            workflow.id,
            version.id,
            WorkflowPlanPut(
                plan_definition=_base_plan(
                    steps=[_tool("step_b", tool_version_id=tv)]
                )
            ),
        )
        assert updated.validation_status == WorkflowVersionValidationStatus.INVALID
        assert updated.validation_report is None
        assert updated.content_hash != old_hash
        assert (
            await WorkflowVersionToolRefRepository(session).list_for_version(version.id)
        ) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_v1_then_v2_deprecates_previous(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        service = WorkflowService(session)
        versions = WorkflowVersionService(session)
        workflow = await service.create(WorkflowCreate(name="Cascade WF"))
        tv = await _seed_tool_version(session)
        v1 = await versions.create_version(
            workflow.id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv), change_summary="v1"),
        )
        await versions.validate(workflow.id, v1.id)
        await versions.publish(workflow.id, v1.id)

        v2 = await versions.create_version(
            workflow.id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv), change_summary="v2"),
        )
        await versions.validate(workflow.id, v2.id)
        await versions.publish(workflow.id, v2.id)

        old = await versions.get_version(workflow.id, v1.id)
        new = await versions.get_version(workflow.id, v2.id)
        wf = await service.get(workflow.id)
        assert old.status == WorkflowVersionStatus.DEPRECATED
        assert new.status == WorkflowVersionStatus.PUBLISHED
        assert wf.current_version_id == v2.id


# ---------------------------------------------------------------------------
# G–J. Concurrency / races
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_version_create_distinct_version_no(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="VerRace WF")
        )
        tv = await _seed_tool_version(session)
        workflow_id = workflow.id
        plan = _tool_plan(tv)

    barrier = asyncio.Barrier(2)

    async def _create(summary: str) -> int | str:
        async with integration_session_factory() as session:
            try:
                await barrier.wait()
                version = await WorkflowVersionService(session).create_version(
                    workflow_id,
                    WorkflowVersionCreate(
                        plan_definition=plan, change_summary=summary
                    ),
                )
                return int(version.version_no)
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{getattr(exc, 'code', type(exc).__name__)}"

    results = await asyncio.gather(_create("a"), _create("b"))
    numbers = sorted(r for r in results if isinstance(r, int))
    errors = [r for r in results if isinstance(r, str)]
    if len(numbers) == 2:
        assert numbers == [1, 2]
    else:
        assert len(numbers) == 1
        assert errors and "RESOURCE_CONFLICT" in errors[0]

    async with integration_session_factory() as session:
        versions, total = await WorkflowVersionRepository(session).list_for_workflow(
            workflow_id
        )
        assert total >= 1
        nos = sorted(v.version_no for v in versions)
        assert nos == list(range(1, len(nos) + 1))
        assert len(nos) == len(set(nos))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_publish_exactly_one_current(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        service = WorkflowService(session)
        versions = WorkflowVersionService(session)
        workflow = await service.create(WorkflowCreate(name="PubRace WF"))
        tv = await _seed_tool_version(session)
        workflow_id = workflow.id
        v2 = await versions.create_version(
            workflow_id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv), change_summary="v2"),
        )
        v3 = await versions.create_version(
            workflow_id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv), change_summary="v3"),
        )
        await versions.validate(workflow_id, v2.id)
        await versions.validate(workflow_id, v3.id)
        v2_id, v3_id = v2.id, v3.id

    barrier = asyncio.Barrier(2)

    async def _publish(version_id: uuid.UUID) -> str:
        async with integration_session_factory() as session:
            try:
                await barrier.wait()
                published = await WorkflowVersionService(session).publish(
                    workflow_id, version_id
                )
                return f"OK:{published.id}"
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{getattr(exc, 'code', type(exc).__name__)}"

    results = await asyncio.gather(_publish(v2_id), _publish(v3_id))
    oks = [r for r in results if r.startswith("OK:")]
    assert len(oks) >= 1

    async with integration_session_factory() as session:
        workflow = await WorkflowRepository(session).get(workflow_id)
        assert workflow is not None
        assert workflow.current_version_id is not None
        current = await WorkflowVersionRepository(session).get(
            workflow.current_version_id
        )
        assert current is not None
        assert current.status == WorkflowVersionStatus.PUBLISHED

        v2 = await WorkflowVersionRepository(session).get(v2_id)
        v3 = await WorkflowVersionRepository(session).get(v3_id)
        assert v2 is not None and v3 is not None
        published = [v for v in (v2, v3) if v.status == WorkflowVersionStatus.PUBLISHED]
        deprecated = [
            v for v in (v2, v3) if v.status == WorkflowVersionStatus.DEPRECATED
        ]
        drafts = [v for v in (v2, v3) if v.status == WorkflowVersionStatus.DRAFT]
        assert len(published) == 1
        assert published[0].id == workflow.current_version_id
        assert len(deprecated) + len(drafts) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validate_vs_plan_save_race_consistent(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        versions = WorkflowVersionService(session)
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="Race Plan WF")
        )
        tv = await _seed_tool_version(session)
        workflow_id = workflow.id
        version = await versions.create_version(
            workflow_id,
            WorkflowVersionCreate(plan_definition=_tool_plan(tv)),
        )
        await versions.validate(workflow_id, version.id)
        version_id = version.id
        old_hash = version.content_hash
        new_plan = _base_plan(steps=[_tool("step_b", tool_version_id=tv)])

    barrier = asyncio.Barrier(2)

    async def validate_side() -> str:
        async with integration_session_factory() as session:
            try:
                await barrier.wait()
                result = await WorkflowVersionService(session).validate(
                    workflow_id, version_id
                )
                return f"VAL:{result.validation_status}:{result.content_hash}"
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{getattr(exc, 'code', type(exc).__name__)}"

    async def put_side() -> str:
        async with integration_session_factory() as session:
            try:
                await barrier.wait()
                result = await WorkflowVersionService(session).put_plan(
                    workflow_id,
                    version_id,
                    WorkflowPlanPut(plan_definition=new_plan),
                )
                return f"PUT:{result.validation_status}:{result.content_hash}"
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{getattr(exc, 'code', type(exc).__name__)}"

    await asyncio.gather(validate_side(), put_side())

    async with integration_session_factory() as session:
        row = await WorkflowVersionRepository(session).get(version_id)
        assert row is not None
        refs = await WorkflowVersionToolRefRepository(session).list_for_version(
            version_id
        )
        report = row.validation_report
        # Never: new Plan + old VALID report + old ToolRefs
        if row.validation_status == WorkflowVersionValidationStatus.VALID:
            assert report is not None
            assert report.get("valid") is True
            assert report.get("content_hash") == row.content_hash
            assert {r.step_key for r in refs} == {
                s["id"]
                for s in (row.plan_definition or {}).get("steps", [])
                if s.get("type") == "TOOL"
            }
        else:
            assert row.validation_status == WorkflowVersionValidationStatus.INVALID
            if report is None:
                assert refs == []
            else:
                assert report.get("content_hash") == row.content_hash
        # Plan save must have applied if put won last, or validate re-ran on new plan.
        assert row.content_hash != old_hash or (
            row.plan_definition.get("steps", [{}])[0].get("id") == "step_a"
            and row.validation_status == WorkflowVersionValidationStatus.VALID
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_inactive_approval_policy_blocks_publish(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        versions = WorkflowVersionService(session)
        workflow = await WorkflowService(session).create(
            WorkflowCreate(name="Stale Policy WF")
        )
        tv = await _seed_tool_version(session)
        policy_id = await _seed_approval_policy(session, status="ACTIVE")
        plan = _base_plan(
            steps=[
                _tool("t1", tool_version_id=tv),
                {
                    "id": "apr1",
                    "name": "apr1",
                    "type": AuthorableStepType.APPROVAL.value,
                    "required": True,
                    "depends_on": ["t1"],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {"approval_policy_id": str(policy_id)},
                },
                _tool("t2", tool_version_id=tv, depends_on=["apr1"]),
            ]
        )
        version = await versions.create_version(
            workflow.id, WorkflowVersionCreate(plan_definition=plan)
        )
        ok = await versions.validate(workflow.id, version.id)
        assert ok.validation_status == WorkflowVersionValidationStatus.VALID

        policy = await ApprovalPolicyRepository(session).get(policy_id)
        assert policy is not None
        policy.status = "INACTIVE"
        await session.commit()

        wf_before = await WorkflowService(session).get(workflow.id)
        with pytest.raises(AppError) as exc:
            await versions.publish(workflow.id, version.id)
        assert exc.value.code == "RESOURCE_CONFLICT"

        after = await versions.get_version(workflow.id, version.id)
        assert after.status == WorkflowVersionStatus.DRAFT
        assert after.validation_status == WorkflowVersionValidationStatus.INVALID
        wf_after = await WorkflowService(session).get(workflow.id)
        assert wf_after.current_version_id == wf_before.current_version_id
