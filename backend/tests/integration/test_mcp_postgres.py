"""PostgreSQL integration tests for MCP registry schema and constraints."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.mcp.normalize import RemoteToolDescriptor, content_hash
from app.models.mcp import MCPServer, MCPTool, MCPToolVersion
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.integration
def test_alembic_upgrade_head(integration_database_url: str) -> None:
    import os

    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_server_code(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    code = f"dup-code-{uuid.uuid4().hex[:8]}"
    async with integration_session_factory() as session:
        repo = MCPServerRepository(session)
        await repo.create(
            code=code,
            name="First",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        await session.commit()

    async with integration_session_factory() as session:
        repo = MCPServerRepository(session)
        with pytest.raises(IntegrityError):
            await repo.create(
                code=code,
                name="Second",
                transport_type="STREAMABLE_HTTP",
                endpoint_url="https://mcp.test/mcp",
            )
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_live_tool_per_server_remote_name(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        servers = MCPServerRepository(session)
        tools = MCPToolRepository(session)
        server = await servers.create(
            code=f"tool-uniq-{uuid.uuid4().hex[:8]}",
            name="Tool Unique",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        await tools.create_tool(mcp_server_id=server.id, remote_name="echo")
        await session.commit()
        server_id = server.id

    async with integration_session_factory() as session:
        tools = MCPToolRepository(session)
        with pytest.raises(IntegrityError):
            await tools.create_tool(mcp_server_id=server_id, remote_name="echo")
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_tool_version_content_hash(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
    }
    desc = RemoteToolDescriptor(
        name="echo",
        description="echo",
        input_schema=schema,
        raw={"name": "echo", "inputSchema": schema},
    )
    digest = content_hash(desc)

    async with integration_session_factory() as session:
        servers = MCPServerRepository(session)
        tools = MCPToolRepository(session)
        server = await servers.create(
            code=f"hash-uniq-{uuid.uuid4().hex[:8]}",
            name="Hash Unique",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await tools.create_tool(mcp_server_id=server.id, remote_name="echo")
        await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=digest,
            validation_status="VALID",
            input_schema=schema,
        )
        await session.commit()
        tool_id = tool.id

    async with integration_session_factory() as session:
        tools = MCPToolRepository(session)
        with pytest.raises(IntegrityError):
            await tools.create_version(
                mcp_tool_id=tool_id,
                version_no=2,
                content_hash=digest,
                validation_status="VALID",
                input_schema=schema,
            )
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_jsonb_input_schema_roundtrip(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    nested_schema = {
        "type": "object",
        "properties": {
            "coords": {
                "type": "object",
                "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            }
        },
    }

    async with integration_session_factory() as session:
        servers = MCPServerRepository(session)
        tools = MCPToolRepository(session)
        server = await servers.create(
            code=f"jsonb-{uuid.uuid4().hex[:8]}",
            name="JSONB",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            transport_config={"retry": {"max": 3}},
        )
        server.capabilities = {"tools": {"listChanged": True}}
        await session.flush()
        tool = await tools.create_tool(mcp_server_id=server.id, remote_name="geo")
        version = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=content_hash(
                RemoteToolDescriptor(
                    name="geo",
                    input_schema=nested_schema,
                    raw={"name": "geo", "inputSchema": nested_schema},
                )
            ),
            validation_status="VALID",
            input_schema=nested_schema,
        )
        await session.commit()
        version_id = version.id
        server_id = server.id

    async with integration_session_factory() as session:
        loaded_server = (
            await session.execute(select(MCPServer).where(MCPServer.id == server_id))
        ).scalar_one()
        loaded_version = (
            await session.execute(
                select(MCPToolVersion).where(MCPToolVersion.id == version_id)
            )
        ).scalar_one()
        assert loaded_server.transport_config == {"retry": {"max": 3}}
        assert loaded_server.capabilities == {"tools": {"listChanged": True}}
        assert loaded_version.input_schema == nested_schema


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_version_increments_after_apply(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    schema_v1 = {"type": "object", "properties": {"a": {"type": "string"}}}
    schema_v2 = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }

    async with integration_session_factory() as session:
        servers = MCPServerRepository(session)
        tools = MCPToolRepository(session)
        server = await servers.create(
            code=f"ver-{uuid.uuid4().hex[:8]}",
            name="Versioning",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await tools.create_tool(mcp_server_id=server.id, remote_name="echo")
        v1 = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=content_hash(
                RemoteToolDescriptor(
                    name="echo",
                    input_schema=schema_v1,
                    raw={"name": "echo", "inputSchema": schema_v1},
                )
            ),
            validation_status="VALID",
            input_schema=schema_v1,
        )
        tool.current_version_id = v1.id
        await session.commit()
        tool_id = tool.id

        v2 = await tools.create_version(
            mcp_tool_id=tool_id,
            version_no=await tools.next_version_no(tool_id),
            content_hash=content_hash(
                RemoteToolDescriptor(
                    name="echo",
                    input_schema=schema_v2,
                    raw={"name": "echo", "inputSchema": schema_v2},
                )
            ),
            validation_status="VALID",
            input_schema=schema_v2,
        )
        tool = (await session.execute(select(MCPTool).where(MCPTool.id == tool_id))).scalar_one()
        tool.current_version_id = v2.id
        await session.commit()

    async with integration_session_factory() as session:
        rows, total = await MCPToolRepository(session).list_versions(
            mcp_tool_id=tool_id,
            page=1,
            page_size=10,
        )
        assert total == 2
        version_nos = sorted(row.version_no for row in rows)
        assert version_nos == [1, 2]
