"""PostgreSQL integration tests for Agent registry foundation."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.domain.enums import AgentStatus, AgentVisibility
from app.repositories.agent import AgentRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.agent import AgentCreate, AgentUpdate, AgentVersionCreate, SelectionSettings
from app.services.agent import AgentService
from app.services.agent_content import agent_version_content_hash
from app.services.agent_version import AgentVersionService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _version_create(**overrides):
    body = {
        "system_instruction": "plan safely",
        "llm_profile_id": uuid.uuid4(),
        "request_schema_version": "1.0",
        "plan_schema_version": "1.0",
        "selection_settings": SelectionSettings(
            auto_select_threshold=0.8,
            confirmation_threshold=0.5,
            max_candidates=3,
        ),
        "planning_settings": {},
        "response_settings": {},
    }
    body.update(overrides)
    return AgentVersionCreate(**body)


@pytest.mark.integration
def test_alembic_agent_registry_downgrade_upgrade(integration_database_url: str) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = integration_database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260907_0002")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_constraints_fk_unique(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        agents = AgentRepository(session)
        agent = await agents.create(
            code=f"agt-{uuid.uuid4().hex[:8]}",
            name="Constraint Agent",
            visibility=AgentVisibility.PRIVATE,
            status=AgentStatus.DRAFT,
        )
        await session.commit()
        agent_id = agent.id
        code = agent.code

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await AgentRepository(session).create(
                code=code,
                name="Dup",
                visibility=AgentVisibility.PRIVATE,
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO agents "
                    "(id, code, name, status, visibility, lock_version) "
                    "VALUES (:id, :code, 'Bad', 'PUBLISHED', 'PRIVATE', 1)"
                ),
                {"id": uuid.uuid4(), "code": f"bad-{uuid.uuid4().hex[:6]}"},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO agents "
                    "(id, code, name, status, visibility, lock_version) "
                    "VALUES (:id, :code, 'BadVis', 'DRAFT', 'PUBLIC', 1)"
                ),
                {"id": uuid.uuid4(), "code": f"vis-{uuid.uuid4().hex[:6]}"},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        versions = AgentVersionRepository(session)
        v1 = await versions.create(
            agent_id=agent_id,
            version_no=1,
            system_instruction="x",
            llm_profile_id=uuid.uuid4(),
            request_schema_version="1.0",
            plan_schema_version="1.0",
            selection_settings={
                "auto_select_threshold": 0.8,
                "confirmation_threshold": 0.5,
                "max_candidates": 1,
            },
            planning_settings={},
            response_settings={},
            content_hash="a" * 64,
        )
        await session.commit()
        version_id = v1.id

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await AgentVersionRepository(session).create(
                agent_id=agent_id,
                version_no=1,
                system_instruction="y",
                llm_profile_id=uuid.uuid4(),
                request_schema_version="1.0",
                plan_schema_version="1.0",
                selection_settings={},
                planning_settings={},
                response_settings={},
                content_hash="b" * 64,
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"srv-{uuid.uuid4().hex[:8]}",
            name="T",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name="echo",
        )
        await AgentToolGrantRepository(session).replace_all(
            version_id,
            [{"mcp_tool_id": tool.id, "effect": "ALLOW"}],
        )
        await session.commit()
        tool_id = tool.id

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO agent_tool_grants "
                    "(agent_version_id, mcp_tool_id, effect, requires_confirmation) "
                    "VALUES (:vid, :tid, 'INHERIT', false)"
                ),
                {"vid": version_id, "tid": tool_id},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO agent_tool_grants "
                    "(agent_version_id, mcp_tool_id, effect, requires_confirmation) "
                    "VALUES (:vid, :tid, 'ALLOW', false)"
                ),
                {"vid": version_id, "tid": uuid.uuid4()},
            )
            await session.commit()
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_patch_cas(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        agent = await AgentService(session).create(
            AgentCreate(name="CAS Agent", visibility=AgentVisibility.PRIVATE)
        )
        agent_id = agent.id

    async def _patch(name: str) -> str | None:
        async with integration_session_factory() as session:
            service = AgentService(session)
            current = await service.get(agent_id)
            try:
                updated = await service.update(
                    agent_id,
                    AgentUpdate(name=name, lock_version=current.lock_version),
                    expected_lock_version=int(current.lock_version),
                )
                return updated.name
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{type(exc).__name__}:{getattr(exc, 'code', '')}"

    results = await asyncio.gather(_patch("A"), _patch("B"))
    successes = [r for r in results if r in {"A", "B"}]
    errors = [r for r in results if r and r.startswith("ERR:")]
    assert len(successes) == 1
    assert len(errors) == 1
    assert "RESOURCE_VERSION_CONFLICT" in errors[0]

    async with integration_session_factory() as session:
        final = await AgentRepository(session).get(agent_id)
        assert final is not None
        assert final.lock_version == 2
        assert final.name in {"A", "B"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_version_create(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        agent = await AgentService(session).create(AgentCreate(name="VerRace"))
        agent_id = agent.id

    async def _create(summary: str) -> int | str:
        async with integration_session_factory() as session:
            try:
                version = await AgentVersionService(session).create_version(
                    agent_id,
                    _version_create(change_summary=summary),
                )
                return int(version.version_no)
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{getattr(exc, 'code', type(exc).__name__)}"

    results = await asyncio.gather(_create("a"), _create("b"))
    numbers = sorted(r for r in results if isinstance(r, int))
    errors = [r for r in results if isinstance(r, str)]
    # Both succeed with 1 and 2 under FOR UPDATE, or one conflict retryable
    if len(numbers) == 2:
        assert numbers == [1, 2]
    else:
        assert len(numbers) == 1
        assert errors and "RESOURCE_CONFLICT" in errors[0]

    async with integration_session_factory() as session:
        versions, total = await AgentVersionRepository(session).list_for_agent(agent_id)
        assert total >= 1
        nos = sorted(v.version_no for v in versions)
        assert nos == list(range(1, len(nos) + 1))
        assert len(nos) == len(set(nos))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_publish(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        service = AgentService(session)
        version_service = AgentVersionService(session)
        agent = await service.create(AgentCreate(name="PubRace"))
        agent_id = agent.id
        v2 = await version_service.create_version(agent_id, _version_create(change_summary="v2"))
        v3 = await version_service.create_version(agent_id, _version_create(change_summary="v3"))
        await version_service.validate(agent_id, v2.id)
        await version_service.validate(agent_id, v3.id)
        v2_id, v3_id = v2.id, v3.id

    async def _publish(version_id: uuid.UUID) -> str:
        async with integration_session_factory() as session:
            try:
                published = await AgentVersionService(session).publish(agent_id, version_id)
                return f"OK:{published.id}"
            except Exception as exc:  # noqa: BLE001
                return f"ERR:{getattr(exc, 'code', type(exc).__name__)}"

    results = await asyncio.gather(_publish(v2_id), _publish(v3_id))
    oks = [r for r in results if r.startswith("OK:")]
    assert len(oks) >= 1

    async with integration_session_factory() as session:
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        assert agent.current_version_id is not None
        current = await AgentVersionRepository(session).get(agent.current_version_id)
        assert current is not None
        assert current.status == "PUBLISHED"

        v2 = await AgentVersionRepository(session).get(v2_id)
        v3 = await AgentVersionRepository(session).get(v3_id)
        assert v2 is not None and v3 is not None
        published = [v for v in (v2, v3) if v.status == "PUBLISHED"]
        deprecated = [v for v in (v2, v3) if v.status == "DEPRECATED"]
        drafts = [v for v in (v2, v3) if v.status == "DRAFT"]
        # One published current; other is DEPRECATED or still DRAFT if race lost early
        assert len(published) == 1
        assert published[0].id == agent.current_version_id
        assert agent.lock_version >= 2
        assert len(deprecated) + len(drafts) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_content_hash_stable() -> None:
    profile = uuid.uuid4()
    settings = {
        "auto_select_threshold": 0.82,
        "confirmation_threshold": 0.6,
        "max_candidates": 5,
    }
    a = agent_version_content_hash(
        system_instruction="hello",
        llm_profile_id=profile,
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings=settings,
        planning_settings={"z": 1, "a": 2},
        response_settings={},
    )
    b = agent_version_content_hash(
        system_instruction="hello",
        llm_profile_id=profile,
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings=dict(reversed(list(settings.items()))),
        planning_settings={"a": 2, "z": 1},
        response_settings={},
    )
    assert a == b
    assert len(a) == 64
