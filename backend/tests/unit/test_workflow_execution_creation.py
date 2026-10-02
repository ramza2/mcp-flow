"""Unit tests for WorkflowExecutionCreationService (Workflow → Execution CREATED)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    MCPToolStatus,
    ResourceGrantResourceType,
    RiskClass,
    UserStatus,
    WorkflowStatus,
    WorkflowVersionStatus,
)
from app.execution.policy_selection import (
    build_workflow_execution_policy_snapshot,
    get_expected_tool_policy_snapshot,
)
from app.models.idempotency import ApiIdempotencyRecord
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import ResourceGrantCreate, RoleCreate, RolePermissionReplaceRequest, UserCreate, UserRoleReplaceRequest
from app.schemas.workflow import WorkflowExecutionCreateRequest, WorkflowUpdate
from app.services.authorization import ResourceGrantService
from app.services.role import RoleService
from app.services.user import UserService
from app.services.workflow import WorkflowService
from app.services.workflow_execution_creation import (
    WorkflowExecutionCreationService,
    normalize_plan_inputs,
)
from app.services.workflow_version import WorkflowVersionService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_creation import (
    _idem_key,
    _install_no_side_effects,
)
from tests.unit.test_workflow_registry import (
    _base_plan,
    _create_draft_version,
    _create_workflow,
    _seed_tool_version,
    _tool,
    _tool_plan,
)


async def _activate_tool(
    session: AsyncSession,
    tool_version_id: uuid.UUID,
    *,
    requires_confirmation: bool = False,
    requires_approval: bool = False,
    approval_policy_id: uuid.UUID | None = None,
    max_attempts: int = 1,
) -> uuid.UUID:
    tools = MCPToolRepository(session)
    tv = await tools.get_version(tool_version_id)
    assert tv is not None
    logical = await tools.get(tv.mcp_tool_id)
    assert logical is not None
    logical.current_version_id = tool_version_id
    logical.status = MCPToolStatus.ACTIVE.value
    await MCPToolPolicyRepository(session).create(
        mcp_tool_id=logical.id,
        risk_class=RiskClass.READ_ONLY.value,
        requires_confirmation=requires_confirmation,
        requires_approval=requires_approval,
        approval_policy_id=approval_policy_id,
        timeout_ms=30_000,
        max_attempts=max_attempts,
        backoff_policy=None,
        max_result_bytes=65536,
        allow_auto_select=True,
        data_classification=None,
        policy_metadata=None,
    )
    await session.flush()
    return logical.id


async def _seed_authorized_workflow_user(
    session: AsyncSession,
    *,
    workflow_id: uuid.UUID,
    tool_ids: list[uuid.UUID],
) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"wf-u-{uuid.uuid4().hex[:8]}",
            display_name="WF Exec User",
            email=f"wf-u-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"wf-r-{uuid.uuid4().hex[:8]}", name="WF Exec Role")
    )
    perms = PermissionRepository(session)
    execute_tool = await perms.get_by_code("mcp.tool.execute")
    execute_wf = await perms.get_by_code("workflow.execute")
    assert execute_tool is not None and execute_wf is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[execute_tool.id, execute_wf.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=1,
    )
    grants = ResourceGrantService(session)
    await grants.create_for_user(
        user.id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.WORKFLOW,
            resource_id=workflow_id,
        ),
    )
    for tool_id in tool_ids:
        await grants.create_for_user(
            user.id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool_id,
            ),
        )
    await session.flush()
    return user.id


async def _publish_and_activate(
    session: AsyncSession,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
) -> None:
    versions = WorkflowVersionService(session)
    await versions.validate(workflow_id, version_id)
    await versions.publish(workflow_id, version_id)
    wf = await WorkflowService(session).get(workflow_id)
    assert wf is not None
    await WorkflowService(session).update(
        workflow_id,
        WorkflowUpdate(status=WorkflowStatus.ACTIVE, lock_version=wf.lock_version),
        expected_lock_version=int(wf.lock_version),
    )


async def _seed_ready_workflow(
    session: AsyncSession,
    *,
    plan: dict[str, Any] | None = None,
    tool_version_id: uuid.UUID | None = None,
    policy_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tv_id = tool_version_id or await _seed_tool_version(session)
    policy_kwargs = policy_kwargs or {}
    tool_id = await _activate_tool(session, tv_id, **policy_kwargs)
    workflow = await _create_workflow(session)
    plan_body = plan if plan is not None else _tool_plan(workflow.id, tv_id)
    version = await _create_draft_version(session, workflow.id, plan=plan_body)
    await _publish_and_activate(session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        session, workflow_id=workflow.id, tool_ids=[tool_id]
    )
    await session.commit()
    return {
        "workflow_id": workflow.id,
        "version_id": version.id,
        "tool_id": tool_id,
        "tool_version_id": tv_id,
        "requester_id": user_id,
    }


async def _create(
    session: AsyncSession,
    ctx: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    inputs: dict[str, Any] | None = None,
    requester_id: uuid.UUID | None = None,
) -> Any:
    return await WorkflowExecutionCreationService(session).create_from_workflow_version(
        workflow_id=ctx["workflow_id"],
        version_id=ctx["version_id"],
        requester_id=requester_id or ctx["requester_id"],
        idempotency_key=idempotency_key or _idem_key(),
        body=WorkflowExecutionCreateRequest(inputs=inputs or {}),
    )


# ---------------------------------------------------------------------------
# Happy path / lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_execution_created_workflow_source(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create(db_session, ctx)
    assert outcome.http_status == 201
    assert outcome.replayed is False
    assert outcome.result.status == ExecutionStatus.CREATED.value
    assert outcome.result.source_type == ExecutionSourceType.WORKFLOW_VERSION.value
    assert outcome.result.trigger_type == "USER"
    assert outcome.result.workflow_version_id == ctx["version_id"]
    assert outcome.result.step_count >= 1

    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    assert execution.agent_request_id is None
    assert execution.agent_version_id is None
    assert execution.workflow_version_id == ctx["version_id"]
    assert execution.policy_snapshot.get("schema_version") == "workflow_execution_policy.v1"


@pytest.mark.asyncio
async def test_plan_inputs_and_secret_ref_snapshot(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    tv_id = await _seed_tool_version(db_session)
    tool_id = await _activate_tool(db_session, tv_id)
    workflow = await _create_workflow(db_session)
    secret_id = uuid.uuid4()
    plan = _tool_plan(workflow.id, tv_id)
    plan["inputs"] = {
        "region": {"type": "string", "required": True, "secret": False},
        "credential": {"type": "object", "required": True, "secret": True},
    }
    plan["steps"][0]["config"]["bindings"] = {
        "location": {"kind": BindingKind.PLAN_INPUT.value, "path": "/region"},
    }
    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    await _publish_and_activate(db_session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        db_session, workflow_id=workflow.id, tool_ids=[tool_id]
    )
    await db_session.commit()
    ctx = {
        "workflow_id": workflow.id,
        "version_id": version.id,
        "requester_id": user_id,
    }
    outcome = await _create(
        db_session,
        ctx,
        inputs={
            "region": "KR",
            "credential": {"kind": BindingKind.SECRET_REF.value, "secret_id": str(secret_id)},
        },
    )
    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    assert execution.input_snapshot["region"] == "KR"
    assert execution.input_snapshot["credential"] == {
        "kind": BindingKind.SECRET_REF.value,
        "secret_id": str(secret_id),
    }


def test_normalize_plan_inputs_rejects_unknown_key() -> None:
    from app.schemas.execution_plan import ExecutionPlanV1, default_plan_limits

    wf_id = uuid.uuid4()
    plan = ExecutionPlanV1.model_validate(
        {
            "schema_version": "1.0",
            "goal": "g",
            "source": {"type": "WORKFLOW", "workflow_id": str(wf_id)},
            "inputs": {"a": {"type": "string", "required": True, "secret": False}},
            "limits": default_plan_limits().model_dump(mode="json"),
            "steps": [
                {
                    "id": "step_a",
                    "name": "step_a",
                    "type": "TOOL",
                    "required": True,
                    "depends_on": [],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "tool_version_id": str(uuid.uuid4()),
                        "bindings": {
                            "location": {"kind": "LITERAL", "value": "x"},
                        },
                    },
                }
            ],
            "completion": {
                "success_policy": "ALL_REQUIRED",
                "response_step_ids": ["step_a"],
            },
        }
    )
    with pytest.raises(AppError) as exc:
        normalize_plan_inputs(plan, {"extra": 1})
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_multi_step_policy_snapshot_per_tool_step(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    tv_id = await _seed_tool_version(db_session)
    tool_id = await _activate_tool(db_session, tv_id)
    workflow = await _create_workflow(db_session)
    plan = _base_plan(
        workflow_id=workflow.id,
        steps=[
            _tool("step_a", tool_version_id=tv_id),
            _tool("step_b", tool_version_id=tv_id, depends_on=["step_a"]),
        ],
    )
    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    await _publish_and_activate(db_session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        db_session, workflow_id=workflow.id, tool_ids=[tool_id]
    )
    await db_session.commit()
    outcome = await _create(
        db_session,
        {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
        },
    )
    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    snap = execution.policy_snapshot
    assert snap["schema_version"] == "workflow_execution_policy.v1"
    tool_steps = snap["tool_steps"]
    assert set(tool_steps.keys()) == {"step_a", "step_b"}
    for step_id in ("step_a", "step_b"):
        entry = tool_steps[step_id]
        assert entry["tool_version_id"] == str(tv_id)
        assert entry["policy"]["tool_policy"]["timeout_ms"] == 30_000
        expected = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id=step_id,
            tool_version_id=tv_id,
        )
        assert expected == entry["policy"]


# ---------------------------------------------------------------------------
# Auth / preconditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_workflow_404(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(db_session)
    with pytest.raises(AppError) as exc:
        await WorkflowExecutionCreationService(db_session).create_from_workflow_version(
            workflow_id=uuid.uuid4(),
            version_id=ctx["version_id"],
            requester_id=ctx["requester_id"],
            idempotency_key=_idem_key(),
            body=WorkflowExecutionCreateRequest(inputs={}),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_forbidden_without_workflow_grant(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(db_session)
    with pytest.raises(AppError) as exc:
        await _create(db_session, ctx, requester_id=uuid.uuid4())
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_workflow_not_active_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    tv_id = await _seed_tool_version(db_session)
    tool_id = await _activate_tool(db_session, tv_id)
    workflow = await _create_workflow(db_session)
    version = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv_id)
    )
    await WorkflowVersionService(db_session).validate(workflow.id, version.id)
    await WorkflowVersionService(db_session).publish(workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        db_session, workflow_id=workflow.id, tool_ids=[tool_id]
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(
            db_session,
            {
                "workflow_id": workflow.id,
                "version_id": version.id,
                "requester_id": user_id,
            },
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_non_current_version_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(db_session)
    v2 = await _create_draft_version(
        db_session,
        ctx["workflow_id"],
        plan=_tool_plan(ctx["workflow_id"], ctx["tool_version_id"]),
    )
    await WorkflowVersionService(db_session).validate(ctx["workflow_id"], v2.id)
    await WorkflowVersionService(db_session).publish(ctx["workflow_id"], v2.id)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(
            db_session,
            {**ctx, "version_id": ctx["version_id"]},
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_tool_policy_requires_confirmation_unsupported(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(
        db_session, policy_kwargs={"requires_confirmation": True}
    )
    with pytest.raises(AppError) as exc:
        await _create(db_session, ctx)
    assert exc.value.status_code == 409
    assert exc.value.code == "WORKFLOW_CONFIRMATION_UNSUPPORTED"


@pytest.mark.asyncio
async def test_tool_policy_requires_approval_dag_wait_unsupported(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    from app.repositories.approval_policy import ApprovalPolicyRepository

    approval = await ApprovalPolicyRepository(db_session).create(
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="Tool Approval",
        status="ACTIVE",
    )
    await db_session.flush()
    ctx = await _seed_ready_workflow(
        db_session,
        policy_kwargs={
            "requires_approval": True,
            "approval_policy_id": approval.id,
        },
    )
    with pytest.raises(AppError) as exc:
        await _create(db_session, ctx)
    assert exc.value.status_code == 409
    assert exc.value.code == "DAG_WAIT_UNSUPPORTED"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_idempotency_key_400(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(db_session)
    with pytest.raises(AppError) as exc:
        await WorkflowExecutionCreationService(db_session).create_from_workflow_version(
            workflow_id=ctx["workflow_id"],
            version_id=ctx["version_id"],
            requester_id=ctx["requester_id"],
            idempotency_key="   ",
            body=WorkflowExecutionCreateRequest(inputs={}),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_idempotency_replay_same_key(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(db_session)
    key = _idem_key()
    first = await _create(db_session, ctx, idempotency_key=key)
    replay = await _create(db_session, ctx, idempotency_key=key)
    assert replay.replayed is True
    assert replay.result.id == first.result.id
    assert replay.http_status == 201


@pytest.mark.asyncio
async def test_idempotency_key_reused_different_inputs_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    tv_id = await _seed_tool_version(db_session)
    tool_id = await _activate_tool(db_session, tv_id)
    workflow = await _create_workflow(db_session)
    plan = _tool_plan(workflow.id, tv_id)
    plan["inputs"] = {
        "region": {"type": "string", "required": False, "secret": False},
    }
    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    await _publish_and_activate(db_session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        db_session, workflow_id=workflow.id, tool_ids=[tool_id]
    )
    await db_session.commit()
    ctx = {
        "workflow_id": workflow.id,
        "version_id": version.id,
        "requester_id": user_id,
    }
    key = _idem_key()
    await _create(db_session, ctx, idempotency_key=key, inputs={"region": "A"})
    with pytest.raises(AppError) as exc:
        await _create(db_session, ctx, idempotency_key=key, inputs={"region": "B"})
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_build_workflow_execution_policy_snapshot_shape() -> None:
    wf_id = uuid.uuid4()
    ver_id = uuid.uuid4()
    snap = build_workflow_execution_policy_snapshot(
        workflow_id=wf_id,
        workflow_version_id=ver_id,
        tool_steps={
            "s1": {
                "tool_version_id": str(uuid.uuid4()),
                "policy": {"tool_policy": {"timeout_ms": 30_000}},
            }
        },
    )
    assert snap["schema_version"] == "workflow_execution_policy.v1"
    assert snap["workflow_id"] == str(wf_id)
    assert snap["workflow_version_id"] == str(ver_id)


@pytest.mark.asyncio
async def test_draft_version_not_publishable_for_execution_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    ctx = await _seed_ready_workflow(db_session)
    draft = await _create_draft_version(
        db_session,
        ctx["workflow_id"],
        plan=_tool_plan(ctx["workflow_id"], ctx["tool_version_id"]),
    )
    await WorkflowVersionService(db_session).validate(ctx["workflow_id"], draft.id)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(
            db_session,
            {**ctx, "version_id": draft.id},
        )
    assert exc.value.status_code == 409
