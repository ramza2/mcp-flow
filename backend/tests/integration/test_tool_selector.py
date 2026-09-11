"""PostgreSQL integration tests for Tool Selector + confidence foundation."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

import httpx
import pytest
from app.agent.tool_selector import ToolSelectorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    ConversationMessageRole,
    MCPServerStatus,
    MCPToolStatus,
    ParameterProvenance,
    ResourceGrantResourceType,
    RiskClass,
    ToolVersionValidationStatus,
    UserStatus,
)
from app.model_provider.client import ModelProviderClient
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.role import PermissionRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.schemas.auth import (
    ResourceGrantCreate,
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.authorization import ResourceGrantService
from app.services.conversation import ConversationService
from app.services.embedding_profile import EmbeddingProfileService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _create_active_profile(session: AsyncSession) -> Any:
    profile = await EmbeddingProfileRepository(session).create(
        code=f"emb-{uuid.uuid4().hex[:8]}",
        name="Active",
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="emb",
        base_url="https://llm.test/v1",
        dimension=4,
        distance_metric="cosine",
        is_active_for_tools=False,
    )
    await session.flush()
    return await EmbeddingProfileService(session).activate_for_tools(
        profile.id,
        expected_lock_version=int(profile.lock_version),
    )


def _provider_client(
    *,
    embed_vectors: dict[str, list[float]] | None = None,
    chat_payload: dict[str, Any] | None = None,
    chat_delay: asyncio.Event | None = None,
    chat_release: asyncio.Event | None = None,
    chat_calls: list[dict[str, Any]] | None = None,
) -> ModelProviderClient:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        path = str(request.url.path)
        if path.endswith("/embeddings"):
            payload = json.loads(request.content.decode("utf-8"))
            data = []
            for i, text_value in enumerate(payload["input"]):
                if embed_vectors and text_value in embed_vectors:
                    vector = embed_vectors[text_value]
                else:
                    digest = hashlib.sha256(text_value.encode()).digest()
                    vector = [((digest[j] / 255.0) * 2 - 1) for j in range(4)]
                data.append({"index": i, "embedding": vector, "object": "embedding"})
            return httpx.Response(
                200,
                json={"object": "list", "data": data, "model": "emb"},
            )
        if path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            if chat_calls is not None:
                chat_calls.append(body)
            if chat_delay is not None:
                chat_delay.set()
            if chat_release is not None:
                # Sync handler cannot await; busy-wait briefly for test release.
                import time

                deadline = time.time() + 5.0
                while not chat_release.is_set() and time.time() < deadline:
                    time.sleep(0.01)
            content = json.dumps(chat_payload or {"candidates": [], "ambiguities": []})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return ModelProviderClient(http=http)


async def _seed_active_tool(
    session: AsyncSession,
    *,
    remote_name: str,
    description: str,
    required: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name=f"Server {remote_name}",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status=MCPServerStatus.ACTIVE.value,
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=remote_name,
        display_name=remote_name,
        tags=["weather"],
        status=MCPToolStatus.ACTIVE.value,
    )
    props = {name: {"type": "string"} for name in (required or ["location"])}
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=hashlib.sha256(remote_name.encode()).hexdigest(),
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description=description,
        input_schema={
            "type": "object",
            "properties": props,
            "required": list(required or ["location"]),
        },
        output_schema={"description": "result"},
    )
    tool.current_version_id = version.id
    await session.flush()
    return server.id, tool.id, version.id


async def _seed_agent_version(
    session: AsyncSession,
    *,
    grants: list[dict[str, Any]],
    selection_settings: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    llm = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Selector LLM",
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="gpt-test",
        base_url="https://llm.test/v1",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Selector Agent",
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="select carefully",
        llm_profile_id=llm.id,
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings=selection_settings
        or {
            "auto_select_threshold": 0.82,
            "confirmation_threshold": 0.60,
            "max_candidates": 12,
        },
        planning_settings={},
        response_settings={},
        content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    await AgentToolGrantRepository(session).replace_all(version.id, grants)
    await session.flush()
    return version.id, agent.id


async def _seed_authorized_user(
    session: AsyncSession, *, tool_ids: list[uuid.UUID]
) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"u-{uuid.uuid4().hex[:8]}",
            display_name="User",
            email=f"u-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"r-{uuid.uuid4().hex[:8]}", name="Role")
    )
    execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
    assert execute is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[execute.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=1,
    )
    grants = ResourceGrantService(session)
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


async def _seed_embedding(
    session: AsyncSession,
    *,
    tool_version_id: uuid.UUID,
    profile_id: uuid.UUID,
    search_text: str,
    embedding: list[float],
) -> None:
    repo = ToolEmbeddingRepository(session)
    content_hash = hashlib.sha256(search_text.encode()).hexdigest()
    row = await repo.upsert_ready_if_current(
        tool_version_id=tool_version_id,
        embedding_profile_id=profile_id,
        search_text=search_text,
        content_hash=content_hash,
        embedding=embedding,
    )
    assert row is not None
    await session.flush()


def _structured(
    *,
    request_text: str,
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": request_text,
            "intent": "날씨 조회",
            "entities": entities
            if entities is not None
            else [
                {
                    "name": "location",
                    "value": "서울",
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                }
            ],
            "constraints": [],
            "expected_outputs": ["날씨"],
            "required_capabilities": ["weather.lookup"],
            "risk_hints": [RiskClass.READ_ONLY.value],
            "missing_inputs": [],
            "ambiguities": [],
            "needs_clarification": False,
        }
    ).model_dump(mode="json")


async def _seed_retrieving_request(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    request_text: str,
    entities: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    conv = await ConversationService(session).create_conversation(
        owner_id=user_id, agent_id=agent_id, title="Sel"
    )
    # Conversation.owner must match requester; agent must match version.
    message = await ConversationService(session).append_message(
        conversation_id=conv.id,
        owner_id=user_id,
        role=ConversationMessageRole.USER,
        content={"text": request_text},
        content_text=request_text,
    )
    # AgentRequest requires conversation owner == requester and matching agent.
    # Update conversation agent ownership: already set.
    request = await AgentRequestService(session).create_received(
        conversation_id=conv.id,
        requester_id=user_id,
        agent_version_id=agent_version_id,
        source_message_id=message.id,
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.RECEIVED],
        new_status=AgentRequestStatus.RETRIEVING,
        analyzed_at=request.created_at,
        extra_values={
            "structured_request": _structured(
                request_text=request_text, entities=entities
            ),
            "structured_request_version": "1.0",
            "missing_fields": [],
        },
    )
    await session.commit()
    return request.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_auto_select_building_parameters(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    query = "서울 날씨 UNIQUE_AUTO_TOKEN"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_a, ver_a = await _seed_active_tool(
            session, remote_name="auto_weather_a", description=query
        )
        _, tool_b, ver_b = await _seed_active_tool(
            session,
            remote_name="auto_weather_b",
            description="other weather UNIQUE_B",
        )
        for tool_id in (tool_a, tool_b):
            await MCPToolPolicyRepository(session).create(
                mcp_tool_id=tool_id,
                risk_class=RiskClass.READ_ONLY.value,
                requires_confirmation=False,
                requires_approval=False,
                approval_policy_id=None,
                timeout_ms=5000,
                max_attempts=1,
                backoff_policy=None,
                max_result_bytes=1024,
                allow_auto_select=True,
                data_classification=None,
                policy_metadata=None,
            )
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_a,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
                {
                    "mcp_tool_id": tool_b,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
            ],
        )
        user_id = await _seed_authorized_user(session, tool_ids=[tool_a, tool_b])
        await _seed_embedding(
            session,
            tool_version_id=ver_a,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_b,
            profile_id=profile.id,
            search_text="other weather UNIQUE_B",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        # Conversation owner must own agent — set agent owner.
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user_id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
        )

    chat_calls: list[dict[str, Any]] = []
    provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
        chat_payload={
            "candidates": [
                {
                    "tool_version_id": str(ver_a),
                    "llm_fit_score": 0.95,
                    "reason_summary": "best weather match",
                },
                {
                    "tool_version_id": str(ver_b),
                    "llm_fit_score": 0.70,
                    "reason_summary": "secondary",
                },
            ],
            "ambiguities": [],
        },
        chat_calls=chat_calls,
    )
    async with integration_session_factory() as session:
        outcome = await ToolSelectorService(
            session, model_provider=provider
        ).select(agent_request_id=request_id)
        assert outcome.decision == "AUTO_SELECT"
        assert outcome.agent_request_status == (
            AgentRequestStatus.BUILDING_PARAMETERS.value
        )
        assert outcome.selected_candidate is not None
        assert outcome.selected_candidate.descriptor.tool_version_id == ver_a

    assert len(chat_calls) == 1
    prompt = chat_calls[0]["messages"][-1]["content"]
    assert str(ver_a) in prompt
    assert str(ver_b) in prompt

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.BUILDING_PARAMETERS.value
        assert row.completed_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_unauthorized_tool_excluded_from_prompt(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    query = "unauthorized weather UNIQUE_UNAUTH"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_auth, ver_auth = await _seed_active_tool(
            session,
            remote_name="auth_weather",
            description="authorized low score weather",
        )
        _, tool_unauth, ver_unauth = await _seed_active_tool(
            session,
            remote_name="unauth_weather",
            description=query,
        )
        for tool_id in (tool_auth, tool_unauth):
            await MCPToolPolicyRepository(session).create(
                mcp_tool_id=tool_id,
                risk_class=RiskClass.READ_ONLY.value,
                requires_confirmation=False,
                requires_approval=False,
                approval_policy_id=None,
                timeout_ms=5000,
                max_attempts=1,
                backoff_policy=None,
                max_result_bytes=1024,
                allow_auto_select=True,
                data_classification=None,
                policy_metadata=None,
            )
        # Agent grants both, but user only authorized for tool_auth.
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_auth,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
                {
                    "mcp_tool_id": tool_unauth,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
            ],
        )
        user_id = await _seed_authorized_user(session, tool_ids=[tool_auth])
        await _seed_embedding(
            session,
            tool_version_id=ver_auth,
            profile_id=profile.id,
            search_text="authorized low score weather",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_unauth,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user_id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
        )

    chat_calls: list[dict[str, Any]] = []
    provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
        chat_payload={
            "candidates": [
                {
                    "tool_version_id": str(ver_auth),
                    "llm_fit_score": 0.9,
                    "reason_summary": "only authorized",
                }
            ],
            "ambiguities": [],
        },
        chat_calls=chat_calls,
    )
    async with integration_session_factory() as session:
        await ToolSelectorService(session, model_provider=provider).select(
            agent_request_id=request_id
        )
    assert len(chat_calls) == 1
    prompt = chat_calls[0]["messages"][-1]["content"]
    assert str(ver_auth) in prompt
    assert str(ver_unauth) not in prompt
    assert "unauth_weather" not in prompt


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_no_candidate_rejected(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    query = "no permission weather UNIQUE_EMPTY"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool, ver = await _seed_active_tool(
            session, remote_name="denied_weather", description=query
        )
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        # User has execute permission but no ResourceGrant on the tool.
        user = await UserService(session).create(
            UserCreate(
                username=f"u-{uuid.uuid4().hex[:8]}",
                display_name="User",
                email=f"u-{uuid.uuid4().hex[:8]}@example.com",
                status=UserStatus.ACTIVE,
            )
        )
        role = await RoleService(session).create(
            RoleCreate(code=f"r-{uuid.uuid4().hex[:8]}", name="Role")
        )
        execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
        assert execute is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[execute.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=1,
        )
        await _seed_embedding(
            session,
            tool_version_id=ver,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user.id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user.id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
        )

    chat_calls: list[dict[str, Any]] = []
    provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
        chat_calls=chat_calls,
    )
    async with integration_session_factory() as session:
        outcome = await ToolSelectorService(
            session, model_provider=provider
        ).select(agent_request_id=request_id)
        assert outcome.decision == "NO_MATCH"
    assert chat_calls == []
    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.REJECTED.value
        assert row.completed_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_confirmation_when_auto_select_disabled(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    query = "confirm weather UNIQUE_CONFIRM"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool, ver = await _seed_active_tool(
            session, remote_name="confirm_weather", description=query
        )
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=5000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=1024,
            allow_auto_select=False,
            data_classification=None,
            policy_metadata=None,
        )
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id = await _seed_authorized_user(session, tool_ids=[tool])
        await _seed_embedding(
            session,
            tool_version_id=ver,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user_id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
        )

    provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
        chat_payload={
            "candidates": [
                {
                    "tool_version_id": str(ver),
                    "llm_fit_score": 0.95,
                    "reason_summary": "high score but policy blocks auto",
                }
            ],
            "ambiguities": [],
        },
    )
    async with integration_session_factory() as session:
        outcome = await ToolSelectorService(
            session, model_provider=provider
        ).select(agent_request_id=request_id)
        assert outcome.decision == "CONFIRM"
    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.WAITING_CONFIRMATION.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_waiting_input_missing_required(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    query = "partial weather UNIQUE_PARTIAL"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool, ver = await _seed_active_tool(
            session,
            remote_name="partial_weather",
            description=query,
            required=["location", "date"],
        )
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=5000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=1024,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id = await _seed_authorized_user(session, tool_ids=[tool])
        await _seed_embedding(
            session,
            tool_version_id=ver,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user_id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
            entities=[
                {
                    "name": "location",
                    "value": "서울",
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                }
            ],
        )

    provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
        chat_payload={
            "candidates": [
                {
                    "tool_version_id": str(ver),
                    "llm_fit_score": 0.95,
                    "reason_summary": "missing date",
                }
            ],
            "ambiguities": [],
        },
    )
    async with integration_session_factory() as session:
        outcome = await ToolSelectorService(
            session, model_provider=provider
        ).select(agent_request_id=request_id)
        assert outcome.decision == "CLARIFY"
        assert "date" in outcome.missing_fields
    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.WAITING_INPUT.value
        assert "date" in row.missing_fields


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_cancel_during_rerank(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    query = "cancel weather UNIQUE_CANCEL"
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool, ver = await _seed_active_tool(
            session, remote_name="cancel_weather", description=query
        )
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=5000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=1024,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
        version_id, agent_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id = await _seed_authorized_user(session, tool_ids=[tool])
        await _seed_embedding(
            session,
            tool_version_id=ver,
            profile_id=profile.id,
            search_text=query,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        agent = await AgentRepository(session).get(agent_id)
        assert agent is not None
        agent.owner_id = user_id
        await session.flush()
        request_id = await _seed_retrieving_request(
            session,
            user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            request_text=query,
        )

    started = asyncio.Event()
    release = asyncio.Event()
    embed_provider = _provider_client(
        embed_vectors={query: [1.0, 0.0, 0.0, 0.0]},
    )
    chat_payload = {
        "candidates": [
            {
                "tool_version_id": str(ver),
                "llm_fit_score": 0.95,
                "reason_summary": "late",
            }
        ],
        "ambiguities": [],
    }

    class _DelayedRerankProvider:
        async def embed_texts(self, *args, **kwargs):  # noqa: ANN002
            return await embed_provider.embed_texts(*args, **kwargs)

        async def generate_json(self, *args, **kwargs):  # noqa: ANN002
            started.set()
            await release.wait()
            return dict(chat_payload)

        async def aclose(self) -> None:
            await embed_provider.aclose()

    provider = _DelayedRerankProvider()

    async def run() -> Exception | None:
        async with integration_session_factory() as session:
            try:
                await ToolSelectorService(
                    session, model_provider=provider  # type: ignore[arg-type]
                ).select(agent_request_id=request_id)
                return None
            except Exception as exc:  # noqa: BLE001
                return exc

    task = asyncio.create_task(run())
    await started.wait()
    async with integration_session_factory() as session:
        await AgentRequestService(session).compare_and_set_status(
            request_id,
            expected_statuses=[AgentRequestStatus.SELECTING],
            new_status=AgentRequestStatus.CANCELLED,
            set_completed_at=True,
        )
        await session.commit()
    release.set()
    err = await task
    assert isinstance(err, AppError)
    assert err.code == "RESOURCE_CONFLICT"
    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.CANCELLED.value
