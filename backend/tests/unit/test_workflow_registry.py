"""Unit tests for Workflow registry foundation (services + SQLite)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    JoinPolicy,
    LoopMode,
    WorkflowStatus,
    WorkflowVersionStatus,
    WorkflowVersionValidationStatus,
    WorkflowVisibility,
)
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
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
    WorkflowUpdate,
    WorkflowVersionCreate,
)
from app.services.workflow import WorkflowService
from app.services.workflow_content import workflow_version_content_hash
from app.services.workflow_version import WorkflowVersionService
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _seed_tool_version(
    session: AsyncSession, *, validation_status: str = "VALID"
) -> uuid.UUID:
    server = await MCPServerRepository(session).create(
        code=f"wf-srv-{uuid.uuid4().hex[:8]}",
        name="Workflow Tool Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status="ACTIVE",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=f"tool_{uuid.uuid4().hex[:6]}",
        display_name="wf_tool",
        tags=[],
        status="ACTIVE",
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status=validation_status,
        remote_description="wf",
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
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="Workflow Approval",
        status=status,
    )
    await session.flush()
    return policy.id


def _base_plan(
    *,
    workflow_id: uuid.UUID,
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
        "goal": "workflow registry fixture",
        "source": {"type": "WORKFLOW", "workflow_id": str(workflow_id)},
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
            "tool_version_id": str(tool_version_id),
            "bindings": bindings
            or {"location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}},
        },
    }


def _tool_plan(workflow_id: uuid.UUID, tool_version_id: uuid.UUID) -> dict[str, Any]:
    return _base_plan(
        workflow_id=workflow_id,
        steps=[_tool("step_a", tool_version_id=tool_version_id)],
    )


def _complex_plan(
    *,
    workflow_id: uuid.UUID,
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
        _tool(
            "body_tool",
            tool_version_id=tool_version_id,
            depends_on=["loop1"],
        ),
        _tool(
            "final",
            tool_version_id=tool_version_id,
            depends_on=["loop1"],
        ),
    ]
    plan = _base_plan(
        workflow_id=workflow_id, steps=steps, limits={"max_parallelism": 4}
    )
    plan["completion"]["response_step_ids"] = ["final"]
    return plan


async def _create_workflow(
    session: AsyncSession, **overrides: Any
) -> Any:
    body = {"name": "Ops Workflow", "description": "ops", "visibility": WorkflowVisibility.PRIVATE}
    body.update(overrides)
    return await WorkflowService(session).create(WorkflowCreate(**body))


async def _create_draft_version(
    session: AsyncSession,
    workflow_id: uuid.UUID,
    plan: dict[str, Any] | None = None,
    **overrides: Any,
) -> Any:
    body: dict[str, Any] = {
        "plan_definition": plan if plan is not None else {},
        "change_summary": "initial",
    }
    body.update(overrides)
    return await WorkflowVersionService(session).create_version(
        workflow_id, WorkflowVersionCreate(**body)
    )


# ---------------------------------------------------------------------------
# Workflow CRUD / lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_create_draft_unique_code(db_session: AsyncSession) -> None:
    a = await _create_workflow(db_session, name="Alpha Flow")
    b = await _create_workflow(db_session, name="Alpha Flow")
    assert a.status == WorkflowStatus.DRAFT
    assert a.visibility == WorkflowVisibility.PRIVATE
    assert a.lock_version == 1
    assert a.current_version_id is None
    assert a.code
    assert a.code != b.code


@pytest.mark.asyncio
async def test_workflow_get_list_filter_search_sort(db_session: AsyncSession) -> None:
    await _create_workflow(db_session, name="Zebra Search")
    await _create_workflow(db_session, name="Alpha Search")
    beta = await _create_workflow(db_session, name="Beta Other")

    service = WorkflowService(db_session)
    got = await service.get(beta.id)
    assert got.name == "Beta Other"

    items, total = await service.list(q="Search", sort="+name")
    assert total >= 2
    names = [w.name for w in items if "Search" in w.name]
    assert names == sorted(names)

    filtered, ftotal = await service.list(status_filter="DRAFT", q="Alpha")
    assert ftotal >= 1
    assert all(w.status == WorkflowStatus.DRAFT for w in filtered)
    assert any(w.name == "Alpha Search" for w in filtered)


@pytest.mark.asyncio
async def test_workflow_patch_optimistic_lock(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session, name="Lock Me")
    service = WorkflowService(db_session)

    updated = await service.update(
        workflow.id,
        WorkflowUpdate(name="Renamed", visibility=WorkflowVisibility.INTERNAL, lock_version=1),
        expected_lock_version=1,
    )
    assert updated.name == "Renamed"
    assert updated.visibility == WorkflowVisibility.INTERNAL
    assert updated.lock_version == 2

    with pytest.raises(AppError) as exc:
        await service.update(
            workflow.id,
            WorkflowUpdate(name="Stale", lock_version=1),
            expected_lock_version=1,
        )
    assert exc.value.code == "RESOURCE_VERSION_CONFLICT"
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_workflow_archived_immutable_and_active_rules(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    service = WorkflowService(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    with pytest.raises(AppError) as active_exc:
        await service.update(
            workflow.id,
            WorkflowUpdate(status=WorkflowStatus.ACTIVE, lock_version=1),
            expected_lock_version=1,
        )
    assert active_exc.value.code == "RESOURCE_CONFLICT"

    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    await versions.validate(workflow.id, version.id)
    published = await versions.publish(workflow.id, version.id)
    assert published.status == WorkflowVersionStatus.PUBLISHED

    refreshed = await service.get(workflow.id)
    assert refreshed.status == WorkflowStatus.DRAFT
    assert refreshed.current_version_id == published.id

    activated = await service.update(
        workflow.id,
        WorkflowUpdate(
            status=WorkflowStatus.ACTIVE, lock_version=refreshed.lock_version
        ),
        expected_lock_version=int(refreshed.lock_version),
    )
    assert activated.status == WorkflowStatus.ACTIVE

    archived = await service.update(
        workflow.id,
        WorkflowUpdate(
            status=WorkflowStatus.ARCHIVED, lock_version=activated.lock_version
        ),
        expected_lock_version=int(activated.lock_version),
    )
    assert archived.status == WorkflowStatus.ARCHIVED

    with pytest.raises(AppError) as revive_exc:
        await service.update(
            workflow.id,
            WorkflowUpdate(
                status=WorkflowStatus.ACTIVE, lock_version=archived.lock_version
            ),
            expected_lock_version=int(archived.lock_version),
        )
    assert revive_exc.value.code == "RESOURCE_CONFLICT"


# ---------------------------------------------------------------------------
# Version create / clone / content hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_numbers_and_source_clone(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    other = await _create_workflow(db_session, name="Other WF")
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)
    plan = _tool_plan(workflow.id, tv)

    v1 = await _create_draft_version(db_session, workflow.id, plan=plan)
    assert v1.version_no == 1
    assert v1.status == WorkflowVersionStatus.DRAFT
    assert v1.validation_status == WorkflowVersionValidationStatus.INVALID

    await versions.validate(workflow.id, v1.id)
    refs_before = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        v1.id
    )
    assert len(refs_before) == 1

    v2 = await versions.create_version(
        workflow.id,
        WorkflowVersionCreate(source_version_id=v1.id, change_summary="clone"),
    )
    assert v2.version_no == 2
    assert v2.status == WorkflowVersionStatus.DRAFT
    assert v2.validation_status == WorkflowVersionValidationStatus.INVALID
    assert v2.validation_report is None
    assert v2.plan_definition == v1.plan_definition
    assert v2.input_schema == v1.input_schema
    assert v2.output_schema == v1.output_schema
    assert v2.policy_defaults == v1.policy_defaults
    assert v2.content_hash == v1.content_hash
    clone_refs = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        v2.id
    )
    assert clone_refs == []

    other_v = await _create_draft_version(
        db_session, other.id, plan=_tool_plan(other.id, tv)
    )
    with pytest.raises(AppError) as exc:
        await versions.create_version(
            workflow.id,
            WorkflowVersionCreate(source_version_id=other_v.id),
        )
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_content_hash_deterministic_and_changes_with_plan(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    tv = await _seed_tool_version(db_session)
    plan = _tool_plan(workflow.id, tv)
    v1 = await _create_draft_version(db_session, workflow.id, plan=plan)

    expected = workflow_version_content_hash(
        plan_schema_version=v1.plan_schema_version,
        plan_definition=v1.plan_definition,
        input_schema=v1.input_schema,
        output_schema=v1.output_schema,
        policy_defaults=v1.policy_defaults,
    )
    assert v1.content_hash == expected
    assert len(v1.content_hash) == 64

    versions = WorkflowVersionService(db_session)
    updated = await versions.put_plan(
        workflow.id,
        v1.id,
        WorkflowPlanPut(
            plan_definition=_base_plan(
                workflow_id=workflow.id,
                steps=[_tool("step_b", tool_version_id=tv)]
            )
        ),
    )
    assert updated.content_hash != expected
    assert updated.validation_status == WorkflowVersionValidationStatus.INVALID
    assert updated.validation_report is None


@pytest.mark.asyncio
async def test_published_and_deprecated_plan_update_rejected(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    v1 = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    await versions.validate(workflow.id, v1.id)
    await versions.publish(workflow.id, v1.id)

    with pytest.raises(AppError) as pub_exc:
        await versions.put_plan(
            workflow.id,
            v1.id,
            WorkflowPlanPut(plan_definition=_tool_plan(workflow.id, tv)),
        )
    assert pub_exc.value.code == "RESOURCE_CONFLICT"

    # Leave v1 PUBLISHED but non-current, then deprecate.
    v2 = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    await versions.validate(workflow.id, v2.id)
    await WorkflowVersionRepository(db_session).mark_published(
        v2, published_at=datetime.now(UTC)
    )
    wf = await WorkflowRepository(db_session).get(workflow.id)
    assert wf is not None
    await WorkflowRepository(db_session).set_current_version(
        workflow.id,
        current_version_id=v2.id,
        expected_lock_version=int(wf.lock_version),
    )
    await db_session.commit()

    deprecated = await versions.deprecate(workflow.id, v1.id)
    assert deprecated.status == WorkflowVersionStatus.DEPRECATED

    with pytest.raises(AppError) as dep_exc:
        await versions.put_plan(
            workflow.id,
            v1.id,
            WorkflowPlanPut(plan_definition=_tool_plan(workflow.id, tv)),
        )
    assert dep_exc.value.code == "RESOURCE_CONFLICT"


# ---------------------------------------------------------------------------
# Draft Plan save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_plan_save_incomplete_and_schema_reject(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    validated = await versions.validate(workflow.id, version.id)
    assert validated.validation_status == WorkflowVersionValidationStatus.VALID
    refs = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        version.id
    )
    assert len(refs) == 1
    old_hash = validated.content_hash

    updated = await versions.put_plan(
        workflow.id,
        version.id,
        WorkflowPlanPut(plan_definition={}, change_summary="wipe"),
    )
    assert updated.plan_definition == {}
    assert updated.validation_status == WorkflowVersionValidationStatus.INVALID
    assert updated.validation_report is None
    assert updated.content_hash != old_hash
    cleared = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        version.id
    )
    assert cleared == []

    with pytest.raises(ValidationError):
        WorkflowPlanPut.model_validate({"plan_definition": ["not", "an", "object"]})


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_malformed_and_valid_tool(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    incomplete = await _create_draft_version(db_session, workflow.id, plan={})
    bad = await versions.validate(workflow.id, incomplete.id)
    assert bad.validation_status == WorkflowVersionValidationStatus.INVALID
    assert bad.validation_report is not None
    assert bad.validation_report["valid"] is False
    assert bad.validation_report["errors"]

    good = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    ok = await versions.validate(workflow.id, good.id)
    assert ok.validation_status == WorkflowVersionValidationStatus.VALID
    assert ok.validation_report is not None
    assert ok.validation_report["valid"] is True
    refs = await WorkflowVersionToolRefRepository(db_session).list_for_version(good.id)
    assert len(refs) == 1
    assert refs[0].step_key == "step_a"
    assert refs[0].mcp_tool_version_id == tv


@pytest.mark.asyncio
async def test_validate_missing_tool_version(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    missing = uuid.uuid4()
    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, missing)
    )
    result = await versions.validate(workflow.id, version.id)
    assert result.validation_status == WorkflowVersionValidationStatus.INVALID
    codes = [e["code"] for e in (result.validation_report or {}).get("errors", [])]
    assert "WORKFLOW_TOOL_VERSION_NOT_FOUND" in codes
    refs = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        version.id
    )
    assert refs == []


@pytest.mark.asyncio
async def test_validate_approval_policy_states(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    missing_plan = _base_plan(
        workflow_id=workflow.id,
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
                "config": {"approval_policy_id": str(uuid.uuid4())},
            },
        ]
    )
    missing_plan["completion"]["response_step_ids"] = ["t1"]
    v_missing = await _create_draft_version(
        db_session, workflow.id, plan=missing_plan
    )
    r1 = await versions.validate(workflow.id, v_missing.id)
    assert r1.validation_status == WorkflowVersionValidationStatus.INVALID
    codes1 = [e["code"] for e in (r1.validation_report or {}).get("errors", [])]
    assert "WORKFLOW_APPROVAL_POLICY_NOT_FOUND" in codes1

    inactive_id = await _seed_approval_policy(db_session, status="INACTIVE")
    inactive_plan = _base_plan(
        workflow_id=workflow.id,
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
                "config": {"approval_policy_id": str(inactive_id)},
            },
        ]
    )
    inactive_plan["completion"]["response_step_ids"] = ["t1"]
    v_inactive = await _create_draft_version(
        db_session, workflow.id, plan=inactive_plan
    )
    r2 = await versions.validate(workflow.id, v_inactive.id)
    assert r2.validation_status == WorkflowVersionValidationStatus.INVALID
    codes2 = [e["code"] for e in (r2.validation_report or {}).get("errors", [])]
    assert "WORKFLOW_APPROVAL_POLICY_INACTIVE" in codes2

    active_id = await _seed_approval_policy(db_session, status="ACTIVE")
    active_plan = _base_plan(
        workflow_id=workflow.id,
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
                "config": {"approval_policy_id": str(active_id)},
            },
            _tool("t2", tool_version_id=tv, depends_on=["apr1"]),
        ]
    )
    v_active = await _create_draft_version(
        db_session, workflow.id, plan=active_plan
    )
    r3 = await versions.validate(workflow.id, v_active.id)
    assert r3.validation_status == WorkflowVersionValidationStatus.VALID


@pytest.mark.asyncio
async def test_validate_loop_body_tool_refs_and_invalid_clears(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    loop_plan = _base_plan(
        workflow_id=workflow.id,
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
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
                    "body_step_ids": ["body"],
                },
            },
            _tool("body", tool_version_id=tv, depends_on=["loop1"]),
        ]
    )
    loop_plan["completion"]["response_step_ids"] = ["loop1"]
    loop_v = await _create_draft_version(db_session, workflow.id, plan=loop_plan)
    ok = await versions.validate(workflow.id, loop_v.id)
    assert ok.validation_status == WorkflowVersionValidationStatus.VALID
    refs = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        loop_v.id
    )
    assert [r.step_key for r in refs] == ["body"]

    cycle_plan = _base_plan(
        workflow_id=workflow.id,
        steps=[
            _tool("a", tool_version_id=tv, depends_on=["b"]),
            _tool("b", tool_version_id=tv, depends_on=["a"]),
        ]
    )
    cycle_v = await _create_draft_version(db_session, workflow.id, plan=cycle_plan)
    bad = await versions.validate(workflow.id, cycle_v.id)
    assert bad.validation_status == WorkflowVersionValidationStatus.INVALID
    codes = [e["code"] for e in (bad.validation_report or {}).get("errors", [])]
    assert "PLAN_CYCLE_DETECTED" in codes
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(cycle_v.id)
    ) == []


# ---------------------------------------------------------------------------
# Publish / deprecate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_rules_and_immutability(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    v1 = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    with pytest.raises(AppError) as inv_exc:
        await versions.publish(workflow.id, v1.id)
    assert inv_exc.value.code == "RESOURCE_CONFLICT"

    await versions.validate(workflow.id, v1.id)
    # Stale report content_hash: mutate hash after VALID report.
    row = await WorkflowVersionRepository(db_session).get(v1.id)
    assert row is not None
    row.content_hash = "0" * 64
    await db_session.flush()
    with pytest.raises(AppError) as stale_exc:
        await versions.publish(workflow.id, v1.id)
    assert stale_exc.value.code == "RESOURCE_CONFLICT"

    stale_after = await versions.get_version(workflow.id, v1.id)
    assert stale_after.status == WorkflowVersionStatus.DRAFT
    assert stale_after.validation_status == WorkflowVersionValidationStatus.INVALID

    # Re-validate restored content, then publish.
    await versions.validate(workflow.id, v1.id)
    published = await versions.publish(workflow.id, v1.id)
    assert published.status == WorkflowVersionStatus.PUBLISHED
    assert published.published_at is not None

    wf = await WorkflowService(db_session).get(workflow.id)
    assert wf.current_version_id == published.id
    assert wf.status == WorkflowStatus.DRAFT

    with pytest.raises(AppError):
        await versions.put_plan(
            workflow.id,
            v1.id,
            WorkflowPlanPut(plan_definition=_tool_plan(workflow.id, tv)),
        )

    v2 = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv), change_summary="v2"
    )
    await versions.validate(workflow.id, v2.id)
    pub2 = await versions.publish(workflow.id, v2.id)
    assert pub2.status == WorkflowVersionStatus.PUBLISHED
    old = await versions.get_version(workflow.id, v1.id)
    assert old.status == WorkflowVersionStatus.DEPRECATED
    wf2 = await WorkflowService(db_session).get(workflow.id)
    assert wf2.current_version_id == pub2.id


@pytest.mark.asyncio
async def test_publish_blocked_when_approval_policy_deactivated(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)
    policy_id = await _seed_approval_policy(db_session, status="ACTIVE")

    plan = _base_plan(
        workflow_id=workflow.id,
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
    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    ok = await versions.validate(workflow.id, version.id)
    assert ok.validation_status == WorkflowVersionValidationStatus.VALID

    policy = await ApprovalPolicyRepository(db_session).get(policy_id)
    assert policy is not None
    policy.status = "INACTIVE"
    await db_session.flush()

    wf_before = await WorkflowService(db_session).get(workflow.id)
    with pytest.raises(AppError) as exc:
        await versions.publish(workflow.id, version.id)
    assert exc.value.code == "RESOURCE_CONFLICT"

    after = await versions.get_version(workflow.id, version.id)
    assert after.status == WorkflowVersionStatus.DRAFT
    assert after.validation_status == WorkflowVersionValidationStatus.INVALID
    wf_after = await WorkflowService(db_session).get(workflow.id)
    assert wf_after.current_version_id == wf_before.current_version_id


@pytest.mark.asyncio
async def test_deprecate_rules(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)

    draft = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    with pytest.raises(AppError) as draft_exc:
        await versions.deprecate(workflow.id, draft.id)
    assert draft_exc.value.code == "RESOURCE_CONFLICT"

    await versions.validate(workflow.id, draft.id)
    published = await versions.publish(workflow.id, draft.id)

    with pytest.raises(AppError) as current_exc:
        await versions.deprecate(workflow.id, published.id)
    assert current_exc.value.code == "RESOURCE_CONFLICT"

    # Non-current PUBLISHED via manual current switch.
    v2 = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    await versions.validate(workflow.id, v2.id)
    await WorkflowVersionRepository(db_session).mark_published(
        v2, published_at=datetime.now(UTC)
    )
    wf = await WorkflowRepository(db_session).get(workflow.id)
    assert wf is not None
    await WorkflowRepository(db_session).set_current_version(
        workflow.id,
        current_version_id=v2.id,
        expected_lock_version=int(wf.lock_version),
    )
    await db_session.commit()

    deprecated = await versions.deprecate(workflow.id, published.id)
    assert deprecated.status == WorkflowVersionStatus.DEPRECATED
    assert deprecated.deprecated_at is not None

    again = await versions.deprecate(workflow.id, published.id)
    assert again.status == WorkflowVersionStatus.DEPRECATED


# ---------------------------------------------------------------------------
# Plan source ownership / integrity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_agent_source_invalid(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)
    plan = _tool_plan(workflow.id, tv)
    plan["source"] = {"type": "AGENT", "agent_version_id": str(uuid.uuid4())}

    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    result = await versions.validate(workflow.id, version.id)
    assert result.validation_status == WorkflowVersionValidationStatus.INVALID
    codes = [e["code"] for e in (result.validation_report or {}).get("errors", [])]
    assert "WORKFLOW_PLAN_SOURCE_INVALID" in codes
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(version.id)
    ) == []


@pytest.mark.asyncio
async def test_validate_wrong_workflow_id_mismatch(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)
    plan = _tool_plan(uuid.uuid4(), tv)

    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    result = await versions.validate(workflow.id, version.id)
    assert result.validation_status == WorkflowVersionValidationStatus.INVALID
    codes = [e["code"] for e in (result.validation_report or {}).get("errors", [])]
    assert "WORKFLOW_PLAN_SOURCE_MISMATCH" in codes
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(version.id)
    ) == []


@pytest.mark.asyncio
async def test_validate_correct_workflow_source_valid(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)
    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    ok = await versions.validate(workflow.id, version.id)
    assert ok.validation_status == WorkflowVersionValidationStatus.VALID
    assert ok.plan_definition["source"]["type"] == "WORKFLOW"
    assert ok.plan_definition["source"]["workflow_id"] == str(workflow.id)
    refs = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        version.id
    )
    assert len(refs) == 1


@pytest.mark.asyncio
async def test_validate_corrupted_json_fields(db_session: AsyncSession) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)
    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    row = await WorkflowVersionRepository(db_session).get(version.id)
    assert row is not None

    row.plan_definition = []  # type: ignore[assignment]
    await db_session.flush()
    bad_plan = await versions.validate(workflow.id, version.id)
    assert bad_plan.validation_status == WorkflowVersionValidationStatus.INVALID
    codes = [e["code"] for e in (bad_plan.validation_report or {}).get("errors", [])]
    assert "PLAN_DEFINITION_INVALID" in codes
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(version.id)
    ) == []

    row = await WorkflowVersionRepository(db_session).get(version.id)
    assert row is not None
    row.plan_definition = _tool_plan(workflow.id, tv)
    row.input_schema = []  # type: ignore[assignment]
    await db_session.flush()
    bad_input = await versions.validate(workflow.id, version.id)
    assert bad_input.validation_status == WorkflowVersionValidationStatus.INVALID
    codes_in = [
        e["code"] for e in (bad_input.validation_report or {}).get("errors", [])
    ]
    assert "WORKFLOW_INPUT_SCHEMA_INVALID" in codes_in
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(version.id)
    ) == []

    row = await WorkflowVersionRepository(db_session).get(version.id)
    assert row is not None
    row.input_schema = {}
    row.output_schema = []  # type: ignore[assignment]
    await db_session.flush()
    bad_out = await versions.validate(workflow.id, version.id)
    assert bad_out.validation_status == WorkflowVersionValidationStatus.INVALID
    codes_out = [
        e["code"] for e in (bad_out.validation_report or {}).get("errors", [])
    ]
    assert "WORKFLOW_OUTPUT_SCHEMA_INVALID" in codes_out

    row = await WorkflowVersionRepository(db_session).get(version.id)
    assert row is not None
    row.output_schema = {}
    row.policy_defaults = []  # type: ignore[assignment]
    await db_session.flush()
    bad_pol = await versions.validate(workflow.id, version.id)
    assert bad_pol.validation_status == WorkflowVersionValidationStatus.INVALID
    codes_pol = [
        e["code"] for e in (bad_pol.validation_report or {}).get("errors", [])
    ]
    assert "WORKFLOW_POLICY_DEFAULTS_INVALID" in codes_pol
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(version.id)
    ) == []


@pytest.mark.asyncio
async def test_validate_invalid_tool_version_status(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session, validation_status="INVALID")
    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    result = await versions.validate(workflow.id, version.id)
    assert result.validation_status == WorkflowVersionValidationStatus.INVALID
    codes = [e["code"] for e in (result.validation_report or {}).get("errors", [])]
    assert "WORKFLOW_TOOL_VERSION_INVALID" in codes
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(version.id)
    ) == []


@pytest.mark.asyncio
async def test_publish_tool_ref_corruption_invalidates_draft(
    db_session: AsyncSession,
) -> None:
    workflow = await _create_workflow(db_session)
    versions = WorkflowVersionService(db_session)
    tv = await _seed_tool_version(db_session)
    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    ok = await versions.validate(workflow.id, version.id)
    assert ok.validation_status == WorkflowVersionValidationStatus.VALID
    refs = await WorkflowVersionToolRefRepository(db_session).list_for_version(
        version.id
    )
    assert len(refs) == 1

    await WorkflowVersionToolRefRepository(db_session).clear(version.id)
    await WorkflowVersionToolRefRepository(db_session).replace_all(
        version.id, [("wrong_step", tv)]
    )
    await db_session.flush()

    wf_before = await WorkflowService(db_session).get(workflow.id)
    with pytest.raises(AppError) as exc:
        await versions.publish(workflow.id, version.id)
    assert exc.value.code == "RESOURCE_CONFLICT"

    after = await versions.get_version(workflow.id, version.id)
    assert after.status == WorkflowVersionStatus.DRAFT
    assert after.validation_status == WorkflowVersionValidationStatus.INVALID
    assert (
        await WorkflowVersionToolRefRepository(db_session).list_for_version(version.id)
    ) == []
    wf_after = await WorkflowService(db_session).get(workflow.id)
    assert wf_after.current_version_id == wf_before.current_version_id
