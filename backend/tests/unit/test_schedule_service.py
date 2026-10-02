"""Unit tests for Schedule registry service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    AgentStatus,
    AgentVersionStatus,
    AgentVersionValidationStatus,
    BindingKind,
    ResourceGrantResourceType,
    ScheduleStatus,
    ScheduleTargetType,
    ScheduleType,
    UserStatus,
    WorkflowVersionStatus,
)
from app.repositories.agent import AgentRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.role import PermissionRepository
from app.repositories.schedule import ScheduleRepository
from app.schemas.auth import (
    ResourceGrantCreate,
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.schedule import ScheduleCreate, ScheduleUpdate
from app.services.authorization import ResourceGrantService
from app.services.role import RoleService
from app.services.schedule import ScheduleService
from app.services.user import UserService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_workflow_execution_creation import (
    _activate_tool,
    _publish_and_activate,
    _seed_ready_workflow,
    _seed_tool_version,
)
from tests.unit.test_workflow_registry import (
    _create_draft_version,
    _create_workflow,
    _tool_plan,
)


def _future_once_expression() -> str:
    future = datetime.now(UTC) + timedelta(days=30)
    return future.strftime("%Y-%m-%dT%H:%M:%S")


def _schedule_body(
    *,
    target_type: ScheduleTargetType,
    target_id: uuid.UUID,
    **overrides: Any,
) -> ScheduleCreate:
    defaults: dict[str, Any] = {
        "name": f"Schedule {uuid.uuid4().hex[:6]}",
        "target_type": target_type,
        "target_id": target_id,
        "schedule_type": ScheduleType.CRON,
        "schedule_expression": "0 9 * * *",
        "timezone": "UTC",
        "inputs": {},
    }
    defaults.update(overrides)
    return ScheduleCreate(**defaults)


async def _seed_schedule_manager(
    session: AsyncSession, *, with_workflow_execute: bool = False
) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"sch-u-{uuid.uuid4().hex[:8]}",
            display_name="Schedule Owner",
            email=f"sch-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"sch-r-{uuid.uuid4().hex[:8]}", name="Schedule Role")
    )
    perms = PermissionRepository(session)
    manage = await perms.get_by_code("schedule.manage")
    assert manage is not None
    permission_ids = [manage.id]
    if with_workflow_execute:
        execute_wf = await perms.get_by_code("workflow.execute")
        assert execute_wf is not None
        permission_ids.append(execute_wf.id)
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=permission_ids),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=1,
    )
    await session.flush()
    return user.id


async def _seed_published_agent_version(
    session: AsyncSession, owner_id: uuid.UUID
) -> uuid.UUID:
    agent = await AgentRepository(session).create(
        code=f"agt-sch-{uuid.uuid4().hex[:8]}",
        name="Schedule Agent",
        owner_id=owner_id,
        status=AgentStatus.ACTIVE.value,
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="run",
        llm_profile_id=uuid.uuid4(),
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings={},
        planning_settings={},
        response_settings={},
        content_hash=uuid.uuid4().hex,
    )
    version.status = AgentVersionStatus.PUBLISHED.value
    version.validation_status = AgentVersionValidationStatus.VALID.value
    await session.flush()
    return version.id


async def _seed_workflow_target(
    session: AsyncSession, owner_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    ctx = await _seed_ready_workflow(session)
    grants = ResourceGrantService(session)
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.WORKFLOW,
            resource_id=ctx["workflow_id"],
        ),
    )
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_TOOL,
            resource_id=ctx["tool_id"],
        ),
    )
    from app.repositories.user import UserRepository

    roles = await UserService(session).list_roles(owner_id)
    role = await RoleService(session).create(
        RoleCreate(code=f"sch-wf-{uuid.uuid4().hex[:8]}", name="WF Exec")
    )
    execute_wf = await PermissionRepository(session).get_by_code("workflow.execute")
    assert execute_wf is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[execute_wf.id]),
        expected_lock_version=1,
    )
    user = await UserRepository(session).get(owner_id)
    assert user is not None
    role_ids = [r.id for r in roles] + [role.id]
    await UserService(session).replace_roles(
        owner_id,
        UserRoleReplaceRequest(role_ids=role_ids),
        expected_lock_version=int(user.lock_version),
    )
    await session.flush()
    return ctx["workflow_id"], ctx["version_id"]


async def _create_agent_schedule(
    session: AsyncSession,
) -> tuple[uuid.UUID, Any]:
    owner_id = await _seed_schedule_manager(session)
    agent_version_id = await _seed_published_agent_version(session, owner_id)
    schedule = await ScheduleService(session).create(
        _schedule_body(
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
        ),
        owner_id=owner_id,
    )
    return owner_id, schedule


@pytest.mark.asyncio
async def test_create_paused_defaults_and_owner(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    assert schedule.status == ScheduleStatus.PAUSED.value
    assert schedule.next_run_at is None
    assert schedule.owner_id == owner_id
    assert schedule.overlap_policy == "SKIP"
    assert schedule.misfire_policy == "SKIP"
    assert schedule.max_catch_up == 1


@pytest.mark.asyncio
async def test_create_pins_agent_version(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    schedule = await ScheduleService(db_session).create(
        _schedule_body(
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
        ),
        owner_id=owner_id,
    )
    assert schedule.target_type == ScheduleTargetType.AGENT_VERSION.value
    assert schedule.agent_version_id == agent_version_id
    assert schedule.workflow_version_id is None


@pytest.mark.asyncio
async def test_create_pins_workflow_version(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session)
    _wf_id, version_id = await _seed_workflow_target(db_session, owner_id)
    schedule = await ScheduleService(db_session).create(
        _schedule_body(
            target_type=ScheduleTargetType.WORKFLOW_VERSION,
            target_id=version_id,
        ),
        owner_id=owner_id,
    )
    assert schedule.workflow_version_id == version_id
    assert schedule.agent_version_id is None


@pytest.mark.asyncio
async def test_create_rejects_draft_workflow_version(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    tv = await _seed_tool_version(db_session)
    tool_id = await _activate_tool(db_session, tv)
    workflow = await _create_workflow(db_session)
    v1 = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    await _publish_and_activate(db_session, workflow.id, v1.id)
    draft_v2 = await _create_draft_version(
        db_session, workflow.id, plan=_tool_plan(workflow.id, tv)
    )
    grants = ResourceGrantService(db_session)
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.WORKFLOW,
            resource_id=workflow.id,
        ),
    )
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_TOOL,
            resource_id=tool_id,
        ),
    )
    with pytest.raises(AppError) as exc:
        await ScheduleService(db_session).create(
            _schedule_body(
                target_type=ScheduleTargetType.WORKFLOW_VERSION,
                target_id=draft_v2.id,
            ),
            owner_id=owner_id,
        )
    assert exc.value.code == "VALIDATION_ERROR"
    assert "PUBLISHED" in exc.value.message


@pytest.mark.asyncio
async def test_create_rejects_deprecated_agent_version(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    version = await AgentVersionRepository(db_session).get(agent_version_id)
    assert version is not None
    version.status = AgentVersionStatus.DEPRECATED.value
    await db_session.flush()
    with pytest.raises(AppError) as exc:
        await ScheduleService(db_session).create(
            _schedule_body(
                target_type=ScheduleTargetType.AGENT_VERSION,
                target_id=agent_version_id,
            ),
            owner_id=owner_id,
        )
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_workflow_missing_execute_grant_forbidden(
    db_session: AsyncSession,
) -> None:
    owner_id = await _seed_schedule_manager(db_session)
    ctx = await _seed_ready_workflow(db_session)
    with pytest.raises(AppError) as exc:
        await ScheduleService(db_session).create(
            _schedule_body(
                target_type=ScheduleTargetType.WORKFLOW_VERSION,
                target_id=ctx["version_id"],
            ),
            owner_id=owner_id,
        )
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_workflow_secret_ref_normalized_plaintext_rejected(
    db_session: AsyncSession,
) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    tv = await _seed_tool_version(db_session)
    tool_id = await _activate_tool(db_session, tv)
    workflow = await _create_workflow(db_session)
    plan = _tool_plan(workflow.id, tv)
    plan["inputs"] = {
        "token": {"type": "string", "required": True, "secret": True},
    }
    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    await _publish_and_activate(db_session, workflow.id, version.id)
    grants = ResourceGrantService(db_session)
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.WORKFLOW,
            resource_id=workflow.id,
        ),
    )
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_TOOL,
            resource_id=tool_id,
        ),
    )
    secret_id = uuid.uuid4()
    schedule = await ScheduleService(db_session).create(
        _schedule_body(
            target_type=ScheduleTargetType.WORKFLOW_VERSION,
            target_id=version.id,
            inputs={
                "token": {
                    "kind": BindingKind.SECRET_REF.value,
                    "secret_id": str(secret_id),
                }
            },
        ),
        owner_id=owner_id,
    )
    assert schedule.input_template["token"] == {
        "kind": BindingKind.SECRET_REF.value,
        "secret_id": str(secret_id),
    }
    with pytest.raises(AppError) as exc:
        await ScheduleService(db_session).create(
            _schedule_body(
                target_type=ScheduleTargetType.WORKFLOW_VERSION,
                target_id=version.id,
                inputs={"token": "plaintext-secret"},
            ),
            owner_id=owner_id,
        )
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_update_optimistic_lock_conflict(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    service = ScheduleService(db_session)
    with pytest.raises(AppError) as exc:
        await service.update(
            schedule.id,
            ScheduleUpdate(name="New", lock_version=99),
            owner_id=owner_id,
            expected_lock_version=99,
        )
    assert exc.value.code == "RESOURCE_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_cross_owner_get_is_404(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    other_id = await _seed_schedule_manager(db_session)
    with pytest.raises(AppError) as exc:
        await ScheduleService(db_session).get(schedule.id, owner_id=other_id)
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_active_config_edit_409_name_ok(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    service = ScheduleService(db_session)
    active = await service.activate(schedule.id, owner_id=owner_id)
    assert active.status == ScheduleStatus.ACTIVE.value

    renamed = await service.update(
        active.id,
        ScheduleUpdate(name="Renamed only", lock_version=active.lock_version),
        owner_id=owner_id,
        expected_lock_version=active.lock_version,
    )
    assert renamed.name == "Renamed only"

    with pytest.raises(AppError) as exc:
        await service.update(
            active.id,
            ScheduleUpdate(
                schedule_expression="0 10 * * *",
                lock_version=renamed.lock_version,
            ),
            owner_id=owner_id,
            expected_lock_version=renamed.lock_version,
        )
    assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_paused_config_edit_clears_next_run_at(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    service = ScheduleService(db_session)
    active = await service.activate(schedule.id, owner_id=owner_id)
    assert active.next_run_at is not None
    paused = await service.pause(active.id, owner_id=owner_id)
    assert paused.next_run_at is not None

    updated = await service.update(
        paused.id,
        ScheduleUpdate(
            schedule_expression="0 11 * * *",
            lock_version=paused.lock_version,
        ),
        owner_id=owner_id,
        expected_lock_version=paused.lock_version,
    )
    assert updated.next_run_at is None


@pytest.mark.asyncio
async def test_pause_preserves_next_run_at(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    service = ScheduleService(db_session)
    active = await service.activate(schedule.id, owner_id=owner_id)
    before = active.next_run_at
    paused = await service.pause(active.id, owner_id=owner_id)
    assert paused.next_run_at == before


@pytest.mark.asyncio
async def test_activate_computes_next_run_at(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    active = await ScheduleService(db_session).activate(
        schedule.id, owner_id=owner_id
    )
    assert active.status == ScheduleStatus.ACTIVE.value
    assert active.next_run_at is not None
    next_run = active.next_run_at
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=UTC)
    assert next_run > datetime.now(UTC)


@pytest.mark.asyncio
async def test_resume_preserves_overdue_next_run_at(db_session: AsyncSession) -> None:
    owner_id, schedule = await _create_agent_schedule(db_session)
    service = ScheduleService(db_session)
    active = await service.activate(schedule.id, owner_id=owner_id)
    overdue = datetime.now(UTC) - timedelta(hours=1)
    repo = ScheduleRepository(db_session)
    row = await repo.get_for_owner(active.id, owner_id)
    assert row is not None
    row.next_run_at = overdue
    await db_session.flush()
    paused = await service.pause(active.id, owner_id=owner_id)
    resumed = await service.resume(paused.id, owner_id=owner_id)
    actual = resumed.next_run_at
    assert actual is not None
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=UTC)
    assert actual == overdue


@pytest.mark.asyncio
async def test_max_catch_up_over_100_rejected(db_session: AsyncSession) -> None:
    service = ScheduleService(db_session)
    with pytest.raises(AppError) as exc:
        service._validate_max_catch_up(101, "SKIP")
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_activate_resume_fail_when_target_deprecated(
    db_session: AsyncSession,
) -> None:
    owner_id = await _seed_schedule_manager(db_session)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    schedule = await ScheduleService(db_session).create(
        _schedule_body(
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
        ),
        owner_id=owner_id,
    )
    service = ScheduleService(db_session)
    await service.activate(schedule.id, owner_id=owner_id)
    version = await AgentVersionRepository(db_session).get(agent_version_id)
    assert version is not None
    version.status = AgentVersionStatus.DEPRECATED.value
    await db_session.flush()
    paused = await service.pause(schedule.id, owner_id=owner_id)
    with pytest.raises(AppError) as exc:
        await service.resume(paused.id, owner_id=owner_id)
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_activate_once_future_ok(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    schedule = await ScheduleService(db_session).create(
        _schedule_body(
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
            schedule_type=ScheduleType.ONCE,
            schedule_expression=_future_once_expression(),
            timezone="UTC",
        ),
        owner_id=owner_id,
    )
    active = await ScheduleService(db_session).activate(schedule.id, owner_id=owner_id)
    assert active.next_run_at is not None
