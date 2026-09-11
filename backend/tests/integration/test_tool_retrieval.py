"""PostgreSQL integration tests for authorized hybrid Tool retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app.core.errors import AppError
from app.domain.enums import (
    AgentToolGrantEffect,
    MCPServerStatus,
    MCPToolStatus,
    ResourceGrantResourceType,
    RiskClass,
    ToolEmbeddingStatus,
    ToolVersionValidationStatus,
    UserStatus,
)
from app.model_provider.client import ModelProviderClient
from app.models.auth import Role
from app.models.mcp import MCPTool
from app.repositories.agent import AgentRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from app.repositories.role import PermissionRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.schemas.auth import (
    ResourceGrantCreate,
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
    UserUpdate,
)
from app.search.tool_retrieval import ToolRetrievalService
from app.services.authorization import AuthorizationResolver, ResourceGrantService
from app.services.embedding_profile import EmbeddingProfileService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _create_active_profile(
    session: AsyncSession, *, model: str = "emb"
) -> Any:
    profile = await EmbeddingProfileRepository(session).create(
        code=f"emb-{uuid.uuid4().hex[:8]}",
        name="Active",
        provider="OPENAI_COMPATIBLE",
        model=model,
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


def _embed_client(
    vectors_by_text: dict[str, list[float]] | None = None,
    *,
    dimension: int = 4,
) -> ModelProviderClient:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content.decode("utf-8"))
        data = []
        for i, text_value in enumerate(payload["input"]):
            if vectors_by_text and text_value in vectors_by_text:
                vector = vectors_by_text[text_value]
            else:
                digest = hashlib.sha256(text_value.encode()).digest()
                vector = [((digest[j] / 255.0) * 2 - 1) for j in range(dimension)]
            data.append({"index": i, "embedding": vector, "object": "embedding"})
        return httpx.Response(
            200,
            json={"object": "list", "data": data, "model": "emb-test"},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return ModelProviderClient(http=http)


async def _seed_active_tool(
    session: AsyncSession,
    *,
    remote_name: str,
    description: str,
    tags: list[str] | None = None,
    server_status: str = MCPServerStatus.ACTIVE.value,
    tool_status: str = MCPToolStatus.ACTIVE.value,
    validation_status: str = ToolVersionValidationStatus.VALID.value,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name=f"Server {remote_name}",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status=server_status,
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=remote_name,
        tags=tags,
        status=tool_status,
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=hashlib.sha256(remote_name.encode()).hexdigest(),
        validation_status=validation_status,
        remote_description=description,
        input_schema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        output_schema={"description": f"{remote_name} result"},
    )
    tool.current_version_id = version.id
    await session.flush()
    return server.id, tool.id, version.id


async def _seed_agent_version(
    session: AsyncSession, *, grants: list[dict[str, Any]]
) -> uuid.UUID:
    llm = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Retrieval LLM",
        provider="OPENAI_COMPATIBLE",
        model="gpt-test",
        base_url="https://llm.test/v1",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Retrieval Agent",
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="retrieve",
        llm_profile_id=llm.id,
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings={"max_candidates": 5},
        planning_settings={},
        response_settings={},
        content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    await AgentToolGrantRepository(session).replace_all(version.id, grants)
    await session.flush()
    return version.id


async def _seed_authorized_user(
    session: AsyncSession,
    *,
    tool_ids: list[uuid.UUID],
    via_role_grant: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
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
        payload = ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_TOOL,
            resource_id=tool_id,
        )
        if via_role_grant:
            await grants.create_for_role(role.id, payload)
        else:
            await grants.create_for_user(user.id, payload)
    await session.flush()
    return user.id, role.id


async def _seed_embedding(
    session: AsyncSession,
    *,
    tool_version_id: uuid.UUID,
    profile_id: uuid.UUID,
    search_text: str,
    embedding: list[float] | None,
    status: str = ToolEmbeddingStatus.READY.value,
) -> None:
    repo = ToolEmbeddingRepository(session)
    content_hash = hashlib.sha256(search_text.encode()).hexdigest()
    if status == ToolEmbeddingStatus.READY.value:
        assert embedding is not None
        row = await repo.upsert_ready_if_current(
            tool_version_id=tool_version_id,
            embedding_profile_id=profile_id,
            search_text=search_text,
            content_hash=content_hash,
            embedding=embedding,
        )
        assert row is not None
    elif status == ToolEmbeddingStatus.FAILED.value:
        await repo.upsert_failed(
            tool_version_id=tool_version_id,
            embedding_profile_id=profile_id,
            search_text=search_text,
            content_hash=content_hash,
        )
    else:
        await repo.upsert_stale(
            tool_version_id=tool_version_id,
            embedding_profile_id=profile_id,
            search_text=search_text,
            content_hash=content_hash,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authorized_hybrid_retrieval_happy_path(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_a, ver_a = await _seed_active_tool(
            session,
            remote_name="weather_lookup",
            description="lookup weather forecast city",
            tags=["weather"],
        )
        _, tool_b, ver_b = await _seed_active_tool(
            session,
            remote_name="calendar_list",
            description="list calendar events",
            tags=["calendar"],
        )
        _, tool_c, ver_c = await _seed_active_tool(
            session,
            remote_name="weather_alert",
            description="severe weather alerts",
            tags=["weather", "alert"],
        )
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {"mcp_tool_id": tool_a, "effect": AgentToolGrantEffect.ALLOW.value},
                {"mcp_tool_id": tool_b, "effect": AgentToolGrantEffect.ALLOW.value},
                {"mcp_tool_id": tool_c, "effect": AgentToolGrantEffect.ALLOW.value},
            ],
        )
        user_id, _ = await _seed_authorized_user(
            session, tool_ids=[tool_a, tool_b, tool_c]
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_a,
            profile_id=profile.id,
            search_text="weather_lookup lookup weather forecast city weather",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_b,
            profile_id=profile.id,
            search_text="calendar_list list calendar events calendar",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_c,
            profile_id=profile.id,
            search_text="weather_alert severe weather alerts weather",
            embedding=[0.9, 0.1, 0.0, 0.0],
        )
        await session.commit()
        profile_id = profile.id

    query = "weather forecast"
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [1.0, 0.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        names = [c.descriptor.name for c in result.candidates]
        assert "weather_lookup" in names
        assert "weather_alert" in names
        assert names[0] in {"weather_lookup", "weather_alert"}
        assert result.embedding_profile_id == profile_id
        assert len(result.candidates) <= 20
        assert result.candidates[0].descriptor.required_inputs == ("q",)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unauthorized_top_rank_excluded_and_server_grant_non_inheritance(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        server_id, tool_secret, ver_secret = await _seed_active_tool(
            session,
            remote_name="top_secret_weather",
            description="perfect weather match secret",
        )
        _, tool_ok, ver_ok = await _seed_active_tool(
            session,
            remote_name="ok_weather",
            description="secondary weather tool",
        )
        _, tool_server_only, ver_server_only = await _seed_active_tool(
            session,
            remote_name="server_only_weather",
            description="server grant only weather",
        )
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_secret,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
                {"mcp_tool_id": tool_ok, "effect": AgentToolGrantEffect.ALLOW.value},
                {
                    "mcp_tool_id": tool_server_only,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
            ],
        )
        user_id, _ = await _seed_authorized_user(session, tool_ids=[tool_ok])
        await ResourceGrantService(session).create_for_user(
            user_id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_SERVER,
                resource_id=server_id,
            ),
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_secret,
            profile_id=profile.id,
            search_text="perfect weather match secret weather",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_ok,
            profile_id=profile.id,
            search_text="secondary weather tool weather",
            embedding=[0.5, 0.5, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_server_only,
            profile_id=profile.id,
            search_text="server grant only weather",
            embedding=[0.8, 0.2, 0.0, 0.0],
        )
        await session.commit()

    query = "perfect weather match"
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [1.0, 0.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        names = [c.descriptor.name for c in result.candidates]
        assert names == ["ok_weather"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_deny_missing_grant_and_role_soft_delete(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_deny, ver_deny = await _seed_active_tool(
            session, remote_name="denied_weather", description="denied weather"
        )
        _, tool_missing, ver_missing = await _seed_active_tool(
            session, remote_name="missing_weather", description="missing weather"
        )
        _, tool_ok, ver_ok = await _seed_active_tool(
            session, remote_name="allowed_weather", description="allowed weather"
        )
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_deny,
                    "effect": AgentToolGrantEffect.DENY.value,
                },
                {"mcp_tool_id": tool_ok, "effect": AgentToolGrantEffect.ALLOW.value},
            ],
        )
        user_id, role_id = await _seed_authorized_user(
            session,
            tool_ids=[tool_deny, tool_missing, tool_ok],
            via_role_grant=True,
        )
        for ver, text in (
            (ver_deny, "denied weather"),
            (ver_missing, "missing weather"),
            (ver_ok, "allowed weather"),
        ):
            await _seed_embedding(
                session,
                tool_version_id=ver,
                profile_id=profile.id,
                search_text=text,
                embedding=[1.0, 0.0, 0.0, 0.0],
            )
        await session.commit()

    query = "weather"
    provider = _embed_client({query: [1.0, 0.0, 0.0, 0.0]})
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=provider
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        assert [c.descriptor.name for c in result.candidates] == ["allowed_weather"]

        decision = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_ok
        )
        assert decision.allowed is True

        await session.execute(
            update(Role).where(Role.id == role_id).values(deleted_at=datetime.now(UTC))
        )
        await session.commit()

        result2 = await ToolRetrievalService(
            session, model_provider=provider
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        assert result2.candidates == ()
        decision2 = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_ok
        )
        assert decision2.allowed is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_policy_verification_embedding_statuses(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_inactive_server, ver_is = await _seed_active_tool(
            session,
            remote_name="inactive_server_weather",
            description="weather inactive server",
            server_status=MCPServerStatus.INACTIVE.value,
        )
        _, tool_inactive, ver_it = await _seed_active_tool(
            session,
            remote_name="inactive_tool_weather",
            description="weather inactive tool",
            tool_status=MCPToolStatus.INACTIVE.value,
        )
        _, tool_invalid, ver_inv = await _seed_active_tool(
            session,
            remote_name="invalid_version_weather",
            description="weather invalid version",
            validation_status=ToolVersionValidationStatus.INVALID.value,
        )
        _, tool_policy, ver_pol = await _seed_active_tool(
            session,
            remote_name="policy_weather",
            description="weather restrictive policy",
        )
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool_policy,
            risk_class=RiskClass.DESTRUCTIVE.value,
            requires_confirmation=True,
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
        await MCPToolVerificationRepository(session).create(
            mcp_tool_version_id=ver_pol,
            status="FAILED",
            criteria_version="v1",
        )
        _, tool_failed, ver_fe = await _seed_active_tool(
            session,
            remote_name="failed_emb_weather",
            description="weather failed embedding lexical",
        )
        _, tool_stale, ver_st = await _seed_active_tool(
            session,
            remote_name="stale_emb_weather",
            description="weather stale embedding",
        )
        _, tool_no_policy, ver_np = await _seed_active_tool(
            session,
            remote_name="no_policy_weather",
            description="weather without policy",
        )
        tool_ids = [
            tool_inactive_server,
            tool_inactive,
            tool_invalid,
            tool_policy,
            tool_failed,
            tool_stale,
            tool_no_policy,
        ]
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {"mcp_tool_id": tid, "effect": AgentToolGrantEffect.ALLOW.value}
                for tid in tool_ids
            ],
        )
        user_id, _ = await _seed_authorized_user(session, tool_ids=tool_ids)
        await _seed_embedding(
            session,
            tool_version_id=ver_is,
            profile_id=profile.id,
            search_text="weather inactive server",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_it,
            profile_id=profile.id,
            search_text="weather inactive tool",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_inv,
            profile_id=profile.id,
            search_text="weather invalid version",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_pol,
            profile_id=profile.id,
            search_text="weather restrictive policy",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_fe,
            profile_id=profile.id,
            search_text="weather failed embedding lexical",
            embedding=None,
            status=ToolEmbeddingStatus.FAILED.value,
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_st,
            profile_id=profile.id,
            search_text="weather stale embedding",
            embedding=None,
            status=ToolEmbeddingStatus.STALE.value,
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_np,
            profile_id=profile.id,
            search_text="weather without policy",
            embedding=[0.8, 0.2, 0.0, 0.0],
        )
        await session.commit()

    query = "weather"
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [1.0, 0.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        by_name = {c.descriptor.name: c for c in result.candidates}
        assert "inactive_server_weather" not in by_name
        assert "inactive_tool_weather" not in by_name
        assert "invalid_version_weather" not in by_name
        assert "stale_emb_weather" not in by_name
        assert "policy_weather" in by_name
        assert by_name["policy_weather"].allow_auto_select is False
        assert (
            by_name["policy_weather"].descriptor.risk_class
            == RiskClass.DESTRUCTIVE.value
        )
        assert "failed_emb_weather" in by_name
        assert by_name["failed_emb_weather"].vector_rank is None
        assert "no_policy_weather" in by_name
        assert (
            by_name["no_policy_weather"].descriptor.risk_class
            == RiskClass.UNKNOWN.value
        )
        assert by_name["no_policy_weather"].policy_present is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_scope_and_activation_race_retry(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile_a = await _create_active_profile(session, model="emb-a")
        profile_b = await EmbeddingProfileRepository(session).create(
            code=f"emb-b-{uuid.uuid4().hex[:8]}",
            name="B",
            provider="OPENAI_COMPATIBLE",
            model="emb-b",
            base_url="https://llm.test/v1",
            dimension=4,
            distance_metric="cosine",
            is_active_for_tools=False,
        )
        _, tool_a, ver_a = await _seed_active_tool(
            session,
            remote_name="profile_a_weather",
            description="profile A only weather",
        )
        _, tool_b, ver_b = await _seed_active_tool(
            session,
            remote_name="profile_b_weather",
            description="profile B only weather UNIQUE_B_TOKEN",
        )
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {"mcp_tool_id": tool_a, "effect": AgentToolGrantEffect.ALLOW.value},
                {"mcp_tool_id": tool_b, "effect": AgentToolGrantEffect.ALLOW.value},
            ],
        )
        user_id, _ = await _seed_authorized_user(
            session, tool_ids=[tool_a, tool_b]
        )
        # Profile A index only for tool_a; Profile B index only for tool_b.
        await _seed_embedding(
            session,
            tool_version_id=ver_a,
            profile_id=profile_a.id,
            search_text="profile A weather",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_b,
            profile_id=profile_b.id,
            search_text="profile B weather UNIQUE_B_TOKEN",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        await session.commit()
        profile_a_id = profile_a.id
        profile_b_id = profile_b.id
        lock_b = int(profile_b.lock_version)

    query = "UNIQUE_B_TOKEN"
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [0.0, 1.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        names = [c.descriptor.name for c in result.candidates]
        # Active profile A must not surface profile-B-only embeddings.
        assert "profile_b_weather" not in names
        assert result.embedding_profile_id == profile_a_id

    gate = asyncio.Event()

    class _DelayedClient(ModelProviderClient):
        def __init__(self) -> None:
            super().__init__(
                http=httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _request: httpx.Response(
                            200,
                            json={
                                "object": "list",
                                "data": [
                                    {
                                        "index": 0,
                                        "embedding": [0.0, 1.0, 0.0, 0.0],
                                        "object": "embedding",
                                    }
                                ],
                                "model": "emb-test",
                            },
                        )
                    ),
                    follow_redirects=False,
                )
            )
            self.calls = 0

        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                gate.set()
                await asyncio.sleep(0.25)
            return await super().embed_texts(target, inputs)

    delayed = _DelayedClient()

    async def _activate_b() -> None:
        await gate.wait()
        async with integration_session_factory() as session:
            await EmbeddingProfileService(session).activate_for_tools(
                profile_b_id,
                expected_lock_version=lock_b,
            )

    async with integration_session_factory() as session:
        activator = asyncio.create_task(_activate_b())
        result = await ToolRetrievalService(
            session, model_provider=delayed
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text="profile B weather UNIQUE_B_TOKEN",
        )
        await activator
        assert result.profile_retried is True
        assert result.embedding_profile_id == profile_b_id
        assert [c.descriptor.name for c in result.candidates] == ["profile_b_weather"]
        assert delayed.calls == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_race_twice_raises_conflict(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        other = await EmbeddingProfileRepository(session).create(
            code=f"emb-other-{uuid.uuid4().hex[:8]}",
            name="Other",
            provider="OPENAI_COMPATIBLE",
            model="emb-other",
            base_url="https://llm.test/v1",
            dimension=4,
            distance_metric="cosine",
            is_active_for_tools=False,
        )
        agent_version_id = await _seed_agent_version(session, grants=[])
        user_id, _ = await _seed_authorized_user(session, tool_ids=[])
        await session.commit()
        profile_id = profile.id
        other_id = other.id

    flip = {"n": 0}

    class _AlwaysStale(ModelProviderClient):
        async def embed_texts(self, target, inputs):  # type: ignore[no-untyped-def]
            flip["n"] += 1
            async with integration_session_factory() as other_session:
                target_id = other_id if flip["n"] % 2 == 1 else profile_id
                current = await EmbeddingProfileRepository(other_session).get(
                    target_id
                )
                assert current is not None
                await EmbeddingProfileService(other_session).activate_for_tools(
                    target_id,
                    expected_lock_version=int(current.lock_version),
                )
            return [[1.0, 0.0, 0.0, 0.0]]

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ToolRetrievalService(
                session, model_provider=_AlwaysStale()
            ).retrieve(
                user_id=user_id,
                agent_version_id=agent_version_id,
                query_text="weather",
            )
        assert exc.value.code == "RESOURCE_CONFLICT"
        assert "changed during retrieval" in exc.value.message



@pytest.mark.integration
@pytest.mark.asyncio
async def test_permission_missing_excludes_even_with_direct_grant(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_id, ver_id = await _seed_active_tool(
            session,
            remote_name="needs_permission_weather",
            description="weather needs execute permission",
        )
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_id,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        # Grant + agent allow, but no mcp.tool.execute permission on any role.
        user = await UserService(session).create(
            UserCreate(
                username=f"u-{uuid.uuid4().hex[:8]}",
                display_name="NoPerm",
                email=f"u-{uuid.uuid4().hex[:8]}@example.com",
                status=UserStatus.ACTIVE,
            )
        )
        await ResourceGrantService(session).create_for_user(
            user.id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool_id,
            ),
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_id,
            profile_id=profile.id,
            search_text="needs permission weather",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await session.commit()
        user_id = user.id
        lock_version = user.lock_version

    query = "weather"
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [1.0, 0.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        assert result.candidates == ()

        decision = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert decision.allowed is False

        # Restore permission via a new role → candidate appears.
        role = await RoleService(session).create(
            RoleCreate(code=f"r-{uuid.uuid4().hex[:8]}", name="Execute")
        )
        execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
        assert execute is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[execute.id]),
            expected_lock_version=1,
        )
        user = await UserService(session).get(user_id)
        assert user is not None
        await UserService(session).replace_roles(
            user_id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=user.lock_version,
        )
        await session.commit()

        result2 = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [1.0, 0.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        assert [c.descriptor.name for c in result2.candidates] == [
            "needs_permission_weather"
        ]
        decision2 = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert decision2.allowed is True
        assert lock_version >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inactive_and_locked_users_excluded(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_id, ver_id = await _seed_active_tool(
            session,
            remote_name="status_gate_weather",
            description="weather status gate",
        )
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_id,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id, _ = await _seed_authorized_user(session, tool_ids=[tool_id])
        await _seed_embedding(
            session,
            tool_version_id=ver_id,
            profile_id=profile.id,
            search_text="status gate weather",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await session.commit()

    query = "weather"
    provider = _embed_client({query: [1.0, 0.0, 0.0, 0.0]})
    for status_value in (UserStatus.INACTIVE.value, UserStatus.LOCKED.value):
        async with integration_session_factory() as session:
            user = await UserService(session).get(user_id)
            assert user is not None
            await UserService(session).update(
                user_id,
                UserUpdate(status=status_value),
                expected_lock_version=user.lock_version,
            )
            await session.commit()

            result = await ToolRetrievalService(
                session, model_provider=provider
            ).retrieve(
                user_id=user_id,
                agent_version_id=agent_version_id,
                query_text=query,
            )
            assert result.candidates == ()
            decision = await AuthorizationResolver(session).authorize_resource(
                user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
            )
            assert decision.allowed is False

    async with integration_session_factory() as session:
        user = await UserService(session).get(user_id)
        assert user is not None
        await UserService(session).update(
            user_id,
            UserUpdate(status=UserStatus.ACTIVE.value),
            expected_lock_version=user.lock_version,
        )
        await session.commit()
        result = await ToolRetrievalService(
            session, model_provider=provider
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        assert [c.descriptor.name for c in result.candidates] == ["status_gate_weather"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authorization_resolver_parity_matrix(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """authorize_resource.allowed == False ⇒ retrieval never returns the tool.

    Agent/lifecycle gates are held open so parity isolates auth predicates.
    """

    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_direct, ver_direct = await _seed_active_tool(
            session,
            remote_name="parity_direct_weather",
            description="parity direct weather",
        )
        _, tool_role, ver_role = await _seed_active_tool(
            session,
            remote_name="parity_role_weather",
            description="parity role weather",
        )
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_direct,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
                {
                    "mcp_tool_id": tool_role,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                },
            ],
        )
        user_direct, _ = await _seed_authorized_user(
            session, tool_ids=[tool_direct], via_role_grant=False
        )
        user_role, role_id = await _seed_authorized_user(
            session, tool_ids=[tool_role], via_role_grant=True
        )
        for ver, text_value in (
            (ver_direct, "parity direct weather"),
            (ver_role, "parity role weather"),
        ):
            await _seed_embedding(
                session,
                tool_version_id=ver,
                profile_id=profile.id,
                search_text=text_value,
                embedding=[1.0, 0.0, 0.0, 0.0],
            )
        await session.commit()

    query = "weather"
    provider = _embed_client({query: [1.0, 0.0, 0.0, 0.0]})

    async def _names(user: uuid.UUID) -> list[str]:
        async with integration_session_factory() as session:
            result = await ToolRetrievalService(
                session, model_provider=provider
            ).retrieve(
                user_id=user,
                agent_version_id=agent_version_id,
                query_text=query,
            )
            return [c.descriptor.name for c in result.candidates]

    async def _allowed(user: uuid.UUID, tool: uuid.UUID) -> bool:
        async with integration_session_factory() as session:
            decision = await AuthorizationResolver(session).authorize_resource(
                user, "mcp.tool.execute", "MCP_TOOL", tool
            )
            return decision.allowed

    assert await _allowed(user_direct, tool_direct) is True
    assert "parity_direct_weather" in await _names(user_direct)

    assert await _allowed(user_role, tool_role) is True
    assert "parity_role_weather" in await _names(user_role)

    # Soft-delete role → role grant/permission evaporate together.
    async with integration_session_factory() as session:
        await session.execute(
            update(Role)
            .where(Role.id == role_id)
            .values(deleted_at=datetime.now(UTC))
        )
        await session.commit()

    assert await _allowed(user_role, tool_role) is False
    assert await _names(user_role) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_current_version_must_belong_to_same_tool(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_a, ver_a = await _seed_active_tool(
            session,
            remote_name="owner_a_weather",
            description="owner A weather",
        )
        _, tool_b, ver_b = await _seed_active_tool(
            session,
            remote_name="owner_b_secret",
            description="owner B secret weather",
        )
        # Point tool A current_version_id at tool B's version (inconsistent pointer).
        tool_a_row = await session.get(MCPTool, tool_a)
        assert tool_a_row is not None
        tool_a_row.current_version_id = ver_b
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_a,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id, _ = await _seed_authorized_user(session, tool_ids=[tool_a])
        # Embeddings exist for B's version text — must not leak as tool A.
        await _seed_embedding(
            session,
            tool_version_id=ver_b,
            profile_id=profile.id,
            search_text="owner B secret weather",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await _seed_embedding(
            session,
            tool_version_id=ver_a,
            profile_id=profile.id,
            search_text="owner A weather",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        await session.commit()

    query = "secret weather"
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [1.0, 0.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        names = [c.descriptor.name for c in result.candidates]
        assert "owner_a_weather" not in names
        assert "owner_b_secret" not in names
        for candidate in result.candidates:
            assert "secret" not in (candidate.descriptor.description or "").lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_historical_valid_current_invalid_excluded(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        profile = await _create_active_profile(session)
        _, tool_id, ver_old = await _seed_active_tool(
            session,
            remote_name="history_weather",
            description="historical valid weather",
            validation_status=ToolVersionValidationStatus.VALID.value,
        )
        tools = MCPToolRepository(session)
        ver_new = await tools.create_version(
            mcp_tool_id=tool_id,
            version_no=2,
            content_hash=hashlib.sha256(b"history-invalid").hexdigest(),
            validation_status=ToolVersionValidationStatus.INVALID.value,
            remote_description="current invalid weather",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            output_schema={"description": "invalid"},
        )
        tool_row = await session.get(MCPTool, tool_id)
        assert tool_row is not None
        tool_row.current_version_id = ver_new.id
        agent_version_id = await _seed_agent_version(
            session,
            grants=[
                {
                    "mcp_tool_id": tool_id,
                    "effect": AgentToolGrantEffect.ALLOW.value,
                }
            ],
        )
        user_id, _ = await _seed_authorized_user(session, tool_ids=[tool_id])
        await _seed_embedding(
            session,
            tool_version_id=ver_old,
            profile_id=profile.id,
            search_text="historical valid weather",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        await session.commit()

    query = "weather"
    async with integration_session_factory() as session:
        result = await ToolRetrievalService(
            session, model_provider=_embed_client({query: [1.0, 0.0, 0.0, 0.0]})
        ).retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query,
        )
        assert result.candidates == ()
