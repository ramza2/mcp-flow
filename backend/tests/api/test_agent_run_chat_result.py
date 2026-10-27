"""Agent Run chat-result API — owner-only Tool payload projection."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.models.execution import Execution, ExecutionStep
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.test_agent_run_live import _seed_runnable


@pytest.mark.asyncio
async def test_chat_result_no_execution_and_foreign_404(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, _ = await _seed_runnable(db_session_factory)
    created = await authenticated_db_client.post(
        "/api/v1/agent-requests",
        json={"agent_id": str(agent_id), "request_text": "반려동물 목록 조회"},
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    empty = await authenticated_db_client.get(
        f"/api/v1/agent-requests/{request_id}/chat-result"
    )
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["available"] is False
    assert body["unavailable_reason"] == "NO_EXECUTION"
    assert body["display_items"] == []

    foreign = await authenticated_db_client.get(
        f"/api/v1/agent-requests/{uuid.uuid4()}/chat-result"
    )
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_chat_result_projects_pets_and_masks_secrets(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_info = await authenticated_db_client.get("/api/v1/auth/session")
    assert session_info.status_code == 200, session_info.text
    owner_id = uuid.UUID(session_info.json()["user"]["id"])

    agent_id, _ = await _seed_runnable(db_session_factory)
    created = await authenticated_db_client.post(
        "/api/v1/agent-requests",
        json={"agent_id": str(agent_id), "request_text": "반려동물 목록 조회"},
    )
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"srv-{uuid.uuid4().hex[:8]}",
            name="Pets Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="http://127.0.0.1:8091",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name=f"listPets.{uuid.uuid4().hex[:6]}",
            display_name="GET /pets",
            tags=["pets"],
            status="ACTIVE",
        )
        version = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="List pets",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "required": ["pets"],
                "properties": {
                    "pets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "status"],
                            "properties": {
                                "name": {"type": "string"},
                                "status": {"type": "string"},
                            },
                        },
                    }
                },
            },
        )
        tool.current_version_id = version.id
        now = datetime.now(UTC)
        execution = Execution(
            id=uuid.uuid4(),
            source_type="AGENT_REQUEST",
            trigger_type="USER",
            requester_id=owner_id,
            agent_request_id=request_id,
            agent_version_id=None,
            status="SUCCEEDED",
            plan_schema_version="1.0",
            plan_snapshot={"steps": []},
            plan_hash="e" * 64,
            input_snapshot={},
            policy_snapshot={},
            requested_at=now,
            finished_at=now,
            lock_version=1,
        )
        session.add(execution)
        await session.flush()
        session.add(
            ExecutionStep(
                id=uuid.uuid4(),
                execution_id=execution.id,
                step_key="list_pets",
                step_type="TOOL",
                mcp_tool_version_id=version.id,
                sequence_hint=1,
                status="SUCCEEDED",
                step_snapshot={},
                result_inline={
                    "content": [{"type": "text", "text": "ok"}],
                    "structured_content": {
                        "pets": [
                            {"id": 1, "name": "Alpha", "status": "available"},
                            {"id": 2, "name": "Beta", "status": "pending"},
                        ],
                        "password": "MUST_NOT_LEAK_SECRET",
                    },
                    "metadata": {},
                    "result_type": None,
                    "duration_ms": 12,
                },
                attempt_count=1,
                finished_at=now,
                lock_version=1,
            )
        )
        await session.commit()
        execution_id = execution.id

    ok = await authenticated_db_client.get(
        f"/api/v1/agent-requests/{request_id}/chat-result"
    )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["available"] is True
    assert payload["execution_id"] == str(execution_id)
    assert payload["execution_status"] == "SUCCEEDED"
    assert payload["tool_name"] == "GET /pets"
    assert [(i["name"], i["status"]) for i in payload["display_items"]] == [
        ("Alpha", "available"),
        ("Beta", "pending"),
    ]
    assert "MUST_NOT_LEAK_SECRET" not in ok.text


@pytest.mark.asyncio
async def test_chat_result_schema_invalid_fail_closed(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_info = await authenticated_db_client.get("/api/v1/auth/session")
    owner_id = uuid.UUID(session_info.json()["user"]["id"])
    agent_id, _ = await _seed_runnable(db_session_factory)
    created = await authenticated_db_client.post(
        "/api/v1/agent-requests",
        json={"agent_id": str(agent_id), "request_text": "조회"},
    )
    request_id = uuid.UUID(created.json()["id"])

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"srv-{uuid.uuid4().hex[:8]}",
            name="Bad Schema Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="http://127.0.0.1:8091",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name=f"bad.{uuid.uuid4().hex[:6]}",
            display_name="Bad Tool",
            status="ACTIVE",
        )
        version = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            validation_status="VALID",
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "required": ["pets"],
                "properties": {"pets": {"type": "array"}},
            },
        )
        now = datetime.now(UTC)
        execution = Execution(
            id=uuid.uuid4(),
            source_type="AGENT_REQUEST",
            trigger_type="USER",
            requester_id=owner_id,
            agent_request_id=request_id,
            status="SUCCEEDED",
            plan_schema_version="1.0",
            plan_snapshot={"steps": []},
            plan_hash="f" * 64,
            input_snapshot={},
            policy_snapshot={},
            requested_at=now,
            finished_at=now,
            lock_version=1,
        )
        session.add(execution)
        await session.flush()
        session.add(
            ExecutionStep(
                id=uuid.uuid4(),
                execution_id=execution.id,
                step_key="bad",
                step_type="TOOL",
                mcp_tool_version_id=version.id,
                sequence_hint=1,
                status="SUCCEEDED",
                step_snapshot={},
                result_inline={
                    "content": [],
                    "structured_content": {"not_pets": True},
                    "metadata": {},
                    "duration_ms": 1,
                },
                attempt_count=1,
                finished_at=now,
                lock_version=1,
            )
        )
        await session.commit()

    result = await authenticated_db_client.get(
        f"/api/v1/agent-requests/{request_id}/chat-result"
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["available"] is False
    assert body["unavailable_reason"] == "SCHEMA_INVALID"
    assert body["display_items"] == []
