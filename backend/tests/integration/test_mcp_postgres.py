"""PostgreSQL integration tests for MCP registry schema and constraints."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.mcp.client import MCPHttpClient
from app.mcp.normalize import RemoteToolDescriptor, content_hash
from app.models.mcp import MCPServer, MCPTool, MCPToolVersion
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.services.mcp_discovery import MCPDiscoveryService
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ECHO_TOOL: dict[str, Any] = {
    "name": "echo",
    "description": "Echo input back",
    "inputSchema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
}

_WEATHER_TOOL: dict[str, Any] = {
    "name": "lookup_weather",
    "description": "Lookup weather for a city",
    "inputSchema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


def _concurrent_discovery_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    method = body.get("method")
    if method == "server/discover":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"tools": {"listChanged": True}},
            },
        )
    if method == "tools/list":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"tools": [_ECHO_TOOL, _WEATHER_TOOL]},
            },
        )
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": body["id"],
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        },
    )


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_atomic_patch_lock(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        repo = MCPServerRepository(session)
        server = await repo.create(
            code=f"lock-{uuid.uuid4().hex[:8]}",
            name="Original",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        await session.commit()
        server_id = server.id
        assert server.lock_version == 1

    async def patch_name(name: str) -> MCPServer | None:
        async with integration_session_factory() as session:
            repo = MCPServerRepository(session)
            updated = await repo.update_atomic(
                server_id,
                expected_lock_version=1,
                name=name,
            )
            if updated is not None:
                await session.commit()
            return updated

    winner_a, winner_b = await asyncio.gather(
        patch_name("Winner A"),
        patch_name("Winner B"),
    )
    winners = [row for row in (winner_a, winner_b) if row is not None]
    losers = [row for row in (winner_a, winner_b) if row is None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].lock_version == 2

    async with integration_session_factory() as session:
        final = await MCPServerRepository(session).get(server_id)
        assert final is not None
        assert final.lock_version == 2
        assert final.name in {"Winner A", "Winner B"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_discovery_apply(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        servers = MCPServerRepository(session)
        server = await servers.create(
            code=f"disc-conc-{uuid.uuid4().hex[:8]}",
            name="Concurrent Discovery",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        await session.commit()
        server_id = server.id

    transport = httpx.MockTransport(_concurrent_discovery_handler)

    async def run_discovery() -> bool:
        async with integration_session_factory() as session:
            http = httpx.AsyncClient(transport=transport)
            client = MCPHttpClient(http=http)
            try:
                service = MCPDiscoveryService(session, client)
                result = await service.discover(server_id, apply_changes=True)
                return result.success
            finally:
                await client.aclose()

    success_a, success_b = await asyncio.gather(run_discovery(), run_discovery())
    assert success_a is True
    assert success_b is True

    async with integration_session_factory() as session:
        tools_repo = MCPToolRepository(session)
        tools, total = await tools_repo.list_tools(
            mcp_server_id=server_id,
            page=1,
            page_size=100,
        )
        assert total == 2
        remote_names = [tool.remote_name for tool in tools]
        assert sorted(remote_names) == ["echo", "lookup_weather"]
        assert len(remote_names) == len(set(remote_names))

        for tool in tools:
            versions, version_total = await tools_repo.list_versions(
                mcp_tool_id=tool.id,
                page=1,
                page_size=100,
            )
            assert version_total == 1
            assert tool.current_version_id == versions[0].id
            assert versions[0].content_hash is not None
