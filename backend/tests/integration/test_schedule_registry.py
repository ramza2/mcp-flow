"""PostgreSQL integration tests for Schedule registry foundation."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from app.core.errors import AppError
from app.domain.enums import (
    AgentStatus,
    AgentVersionStatus,
    AgentVersionValidationStatus,
    BindingKind,
    ResourceGrantResourceType,
    ScheduleTargetType,
    ScheduleType,
    WorkflowVersionStatus,
)
from app.repositories.agent import AgentRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.schemas.auth import ResourceGrantCreate
from app.schemas.schedule import ScheduleCreate
from app.schemas.workflow import WorkflowVersionCreate
from app.services.authorization import ResourceGrantService
from app.services.schedule import ScheduleService
from app.services.workflow_version import WorkflowVersionService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_schedule_service import (
    _schedule_body,
    _seed_published_agent_version,
    _seed_schedule_manager,
)
from tests.unit.test_workflow_execution_creation import (
    _activate_tool,
    _publish_and_activate,
    _seed_tool_version,
)
from tests.unit.test_workflow_registry import (
    _create_draft_version,
    _create_workflow,
    _tool_plan,
)


@pytest.mark.integration
def test_alembic_schedule_migration_objects_and_permission(
    integration_database_url: str,
) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = integration_database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")

    import sqlalchemy as sa
    from sqlalchemy import create_engine, inspect

    engine = create_engine(integration_database_url.replace("+asyncpg", ""))
    insp = inspect(engine)
    assert "schedules" in insp.get_table_names()
    assert "schedule_occurrences" in insp.get_table_names()
    uqs = {
        c["name"]
        for c in insp.get_unique_constraints("schedule_occurrences")
    }
    assert "uq_schedule_occurrences_schedule_scheduled_for" in uqs
    indexes = {i["name"] for i in insp.get_indexes("schedules")}
    assert "ix_schedules_active_next_run_at" in indexes

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT code FROM permissions WHERE code = 'schedule.manage'")
        ).fetchone()
        assert row is not None
    engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_fails_when_pinned_version_deprecated(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        tv = await _seed_tool_version(session)
        workflow = await _create_workflow(session)
        v1 = await _create_draft_version(
            session, workflow.id, plan=_tool_plan(workflow.id, tv)
        )
        await _publish_and_activate(session, workflow.id, v1.id)
        tool_id = await _activate_tool(session, tv)
        grants = ResourceGrantService(session)
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
        schedule = await ScheduleService(session).create(
            _schedule_body(
                target_type=ScheduleTargetType.WORKFLOW_VERSION,
                target_id=v1.id,
            ),
            owner_id=owner_id,
        )
        await ScheduleService(session).activate(schedule.id, owner_id=owner_id)
        v2 = await WorkflowVersionService(session).create_version(
            workflow.id,
            WorkflowVersionCreate(
                plan_definition=_tool_plan(workflow.id, tv),
                change_summary="v2",
            ),
        )
        await WorkflowVersionService(session).validate(workflow.id, v2.id)
        await WorkflowVersionService(session).publish(workflow.id, v2.id)
        v1_row = await WorkflowVersionService(session).get_version(workflow.id, v1.id)
        assert v1_row.status == WorkflowVersionStatus.DEPRECATED.value
        paused = await ScheduleService(session).pause(schedule.id, owner_id=owner_id)
        with pytest.raises(AppError) as exc:
            await ScheduleService(session).resume(paused.id, owner_id=owner_id)
        assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_create_planned_uniqueness(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session)
        agent_version_id = await _seed_published_agent_version(session, owner_id)
        schedule = await ScheduleService(session).create(
            _schedule_body(
                target_type=ScheduleTargetType.AGENT_VERSION,
                target_id=agent_version_id,
            ),
            owner_id=owner_id,
        )
        schedule_id = schedule.id
        when = datetime.now(UTC) + timedelta(days=1)
        await session.commit()

    async def _insert() -> uuid.UUID:
        async with integration_session_factory() as session:
            repo = ScheduleOccurrenceRepository(session)
            occ = await repo.create_planned(schedule_id, when)
            await session.commit()
            return occ.id

    ids = await asyncio.gather(_insert(), _insert())
    assert ids[0] == ids[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_owner_isolation_404(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner_a = await _seed_schedule_manager(session)
        owner_b = await _seed_schedule_manager(session)
        agent_version_id = await _seed_published_agent_version(session, owner_a)
        schedule = await ScheduleService(session).create(
            _schedule_body(
                target_type=ScheduleTargetType.AGENT_VERSION,
                target_id=agent_version_id,
            ),
            owner_id=owner_a,
        )
        with pytest.raises(AppError) as exc:
            await ScheduleService(session).get(schedule.id, owner_id=owner_b)
        assert exc.value.code == "NOT_FOUND"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_secret_ref_persisted_not_plaintext(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        tv = await _seed_tool_version(session)
        tool_id = await _activate_tool(session, tv)
        workflow = await _create_workflow(session)
        plan = _tool_plan(workflow.id, tv)
        plan["inputs"] = {
            "token": {"type": "string", "required": True, "secret": True},
        }
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        grants = ResourceGrantService(session)
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
        schedule = await ScheduleService(session).create(
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
        await session.commit()
        schedule_id = schedule.id

    async with integration_session_factory() as session:
        raw = (
            await session.execute(
                text(
                    "SELECT input_template::text FROM schedules WHERE id = :id"
                ),
                {"id": str(schedule_id)},
            )
        ).scalar_one()
        assert "plaintext" not in raw
        assert str(secret_id) in raw
        assert BindingKind.SECRET_REF.value in raw
