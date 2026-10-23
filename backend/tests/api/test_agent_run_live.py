"""Real AgentRun phase 1 — owner-scoped persistence and actual Analyzer path.

The model provider is mocked at the external HTTP boundary; these tests are
not claims of actual ModelOps performance or MCP execution.
"""

from __future__ import annotations

import uuid

import pytest
from app.model_provider.client import ModelProviderClient
from app.models.agent import AgentVersion
from app.models.model_profile import LLMProfile
from app.repositories.agent import AgentRepository
from app.repositories.agent_version import AgentVersionRepository
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed_runnable(
    factory: async_sessionmaker[AsyncSession],
    *,
    visibility: str = "INTERNAL",
    owner_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session:
        profile = LLMProfile(
            code=f"profile-{uuid.uuid4().hex[:10]}",
            name="Mocked ModelOps profile",
            provider="OPENAI_COMPATIBLE",
            model="qwen3-14b",
            base_url="https://modelops-gateway.openlink.kr/v1",
        )
        session.add(profile)
        await session.flush()
        agent = await AgentRepository(session).create(
            code=f"agent-{uuid.uuid4().hex[:10]}",
            name="Real Analysis Agent",
            status="ACTIVE",
            visibility=visibility,
            owner_id=owner_id,
        )
        version = await AgentVersionRepository(session).create(
            agent_id=agent.id,
            version_no=2,
            system_instruction="Structure the user's request only.",
            llm_profile_id=profile.id,
            request_schema_version="1.0",
            plan_schema_version="1.0",
            selection_settings={},
            planning_settings={},
            response_settings={},
            content_hash="a" * 64,
        )
        version.status = "PUBLISHED"
        agent.current_version_id = version.id
        await session.commit()
        return agent.id, version.id


@pytest.mark.asyncio
async def test_live_agent_run_persists_then_analyzes_only_once(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id, version_id = await _seed_runnable(db_session_factory)
    sent: list[str] = []

    async def fake_generate(self, target, *, messages, parameters=None):
        sent.append(messages[-1]["content"])
        return {
            "intent": "Find project documents",
            "entities": [],
            "constraints": [],
            "expected_outputs": ["Found documents"],
            "required_capabilities": ["document.lookup"],
            "risk_hints": ["READ_ONLY"],
            "missing_inputs": [],
            "ambiguities": [],
            "needs_clarification": False,
        }

    monkeypatch.setattr(ModelProviderClient, "generate_json", fake_generate)

    agents = await authenticated_db_client.get("/api/v1/agent-requests/available-agents")
    assert agents.status_code == 200
    assert any(item["id"] == str(agent_id) for item in agents.json()["items"])

    response = await authenticated_db_client.post(
        "/api/v1/agent-requests",
        json={"agent_id": str(agent_id), "request_text": "프로젝트 문서를 검색해줘"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "RECEIVED"
    assert created["agent_version_id"] == str(version_id)
    assert created["structured_request"] is None
    request_id = created["id"]

    # No asynchronous model call or Tool call at submission time.
    assert not sent

    analyzed = await authenticated_db_client.post(f"/api/v1/agent-requests/{request_id}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()["status"] == "RETRIEVING"
    assert analyzed.json()["structured_request"]["intent"] == "Find project documents"
    assert sent == ["프로젝트 문서를 검색해줘"]

    persisted = await authenticated_db_client.get(f"/api/v1/agent-requests/{request_id}")
    assert persisted.status_code == 200
    assert persisted.json()["status"] == "RETRIEVING"

    latest = await authenticated_db_client.get(
        f"/api/v1/agent-requests/conversations/{created['conversation_id']}/latest"
    )
    assert latest.status_code == 200
    assert latest.json()["id"] == request_id

    duplicate = await authenticated_db_client.post(
        f"/api/v1/agent-requests/{request_id}/analyze"
    )
    assert duplicate.status_code == 409
    assert sent == ["프로젝트 문서를 검색해줘"]


@pytest.mark.asyncio
async def test_live_agent_run_rejects_unpublished_and_blank_prompt(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, _ = await _seed_runnable(db_session_factory)
    invalid = await authenticated_db_client.post(
        "/api/v1/agent-requests",
        json={"agent_id": str(agent_id), "request_text": "  "},
    )
    assert invalid.status_code == 422
    async with db_session_factory() as session:
        version = (await session.execute(
            select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        )).scalar_one()
        version.status = "DRAFT"
        await session.commit()
    agents = await authenticated_db_client.get("/api/v1/agent-requests/available-agents")
    assert not any(a["id"] == str(agent_id) for a in agents.json()["items"])
    response = await authenticated_db_client.post(
        "/api/v1/agent-requests",
        json={"agent_id": str(agent_id), "request_text": "문서를 검색해줘"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_live_agent_run_private_other_owner_is_hidden_and_rejected(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    other_id = uuid.uuid4()
    agent_id, _ = await _seed_runnable(
        db_session_factory, visibility="PRIVATE", owner_id=other_id
    )
    agents = await authenticated_db_client.get("/api/v1/agent-requests/available-agents")
    assert not any(a["id"] == str(agent_id) for a in agents.json()["items"])
    response = await authenticated_db_client.post(
        "/api/v1/agent-requests",
        json={"agent_id": str(agent_id), "request_text": "개인 문서 검색"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unowned_agent_run_request_does_not_leak(
    authenticated_db_client: AsyncClient,
) -> None:
    missing_id = uuid.uuid4()
    response = await authenticated_db_client.get(
        f"/api/v1/agent-requests/{missing_id}"
    )
    assert response.status_code == 404
    response = await authenticated_db_client.post(
        f"/api/v1/agent-requests/{missing_id}/analyze"
    )
    assert response.status_code == 404
