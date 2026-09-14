"""API tests for Clarification response endpoint (Session + CSRF + ownership)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.domain.enums import AgentRequestStatus, ClarificationRequestStatus
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_parameter_builder import _seed_building_parameters

API = "/api/v1/agent-requests"


async def _seed_waiting_for_owner(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
) -> dict[str, Any]:
    seeded = await _seed_building_parameters(
        session,
        required=["location"],
        entities=[],
    )
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    request.requester_id = requester_id
    await session.flush()
    structured = StructuredRequestV1.model_validate(request.structured_request)
    patched = StructuredRequestV1.model_validate(
        {
            **structured.model_dump(mode="json"),
            "missing_inputs": ["location"],
            "ambiguities": [],
            "needs_clarification": True,
        }
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
        new_status=AgentRequestStatus.BUILDING_PARAMETERS,
        extra_values={
            "structured_request": patched.model_dump(mode="json"),
            "structured_request_version": "1.0",
        },
    )
    await session.commit()
    outcome = await ParameterBuilderService(session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.agent_request_status == AgentRequestStatus.WAITING_INPUT
    clarification = await ClarificationRequestRepository(
        session
    ).get_open_for_agent_request(seeded["request_id"])
    assert clarification is not None
    return {
        "request_id": seeded["request_id"],
        "clarification_id": clarification.id,
    }


def _path(request_id: uuid.UUID, clarification_id: uuid.UUID) -> str:
    return f"{API}/{request_id}/clarifications/{clarification_id}/responses"


@pytest.mark.asyncio
async def test_unauthenticated_post_401(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        seeded = await _seed_building_parameters(session, entities=[], required=["location"])
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        structured = StructuredRequestV1.model_validate(request.structured_request)
        patched = StructuredRequestV1.model_validate(
            {
                **structured.model_dump(mode="json"),
                "missing_inputs": ["location"],
                "needs_clarification": True,
            }
        )
        request.structured_request = patched.model_dump(mode="json")
        await session.commit()
        await ParameterBuilderService(session).build(agent_request_id=seeded["request_id"])
        clarification = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert clarification is not None
        path = _path(seeded["request_id"], clarification.id)

    response = await unauthenticated_db_client.post(
        path, json={"response_payload": {"location": "서울"}}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_missing_csrf_rejected(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_info = await authenticated_db_client.get("/api/v1/auth/session")
    assert session_info.status_code == 200
    owner_id = uuid.UUID(session_info.json()["user"]["id"])

    async with db_session_factory() as session:
        seeded = await _seed_waiting_for_owner(session, requester_id=owner_id)

    client = authenticated_db_client
    client.headers.pop("X-CSRF-Token", None)
    response = await client.post(
        _path(seeded["request_id"], seeded["clarification_id"]),
        json={"response_payload": {"location": "서울"}},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_CSRF_INVALID"


@pytest.mark.asyncio
async def test_non_owner_forbidden(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        # Seed with a different requester than the authenticated client.
        seeded = await _seed_building_parameters(
            session, entities=[], required=["location"]
        )
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        structured = StructuredRequestV1.model_validate(request.structured_request)
        patched = StructuredRequestV1.model_validate(
            {
                **structured.model_dump(mode="json"),
                "missing_inputs": ["location"],
                "needs_clarification": True,
            }
        )
        request.structured_request = patched.model_dump(mode="json")
        await session.commit()
        await ParameterBuilderService(session).build(
            agent_request_id=seeded["request_id"]
        )
        clarification = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert clarification is not None
        path = _path(seeded["request_id"], clarification.id)
        clarification_id = clarification.id
        request_id = seeded["request_id"]

    response = await authenticated_db_client.post(
        path,
        json={"response_payload": {"location": "서울"}},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"

    async with db_session_factory() as session:
        clarification = await ClarificationRequestRepository(session).get(
            clarification_id
        )
        assert clarification is not None
        assert clarification.status == ClarificationRequestStatus.OPEN.value
        request = await AgentRequestRepository(session).get(request_id)
        assert request is not None
        assert request.status == AgentRequestStatus.WAITING_INPUT.value


@pytest.mark.asyncio
async def test_owner_success_and_replay_conflict(
    authenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session_info = await authenticated_db_client.get("/api/v1/auth/session")
    assert session_info.status_code == 200
    owner_id = uuid.UUID(session_info.json()["user"]["id"])

    async with db_session_factory() as session:
        seeded = await _seed_waiting_for_owner(session, requester_id=owner_id)

    path = _path(seeded["request_id"], seeded["clarification_id"])
    first = await authenticated_db_client.post(
        path,
        json={"response_payload": {"location": "서울"}},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["agent_request_id"] == str(seeded["request_id"])
    assert body["clarification_id"] == str(seeded["clarification_id"])
    assert body["clarification_status"] == "ANSWERED"
    assert body["agent_request_status"] == "RETRIEVING"

    second = await authenticated_db_client.post(
        path,
        json={"response_payload": {"location": "부산"}},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "RESOURCE_CONFLICT"

    async with db_session_factory() as session:
        clarification = await ClarificationRequestRepository(session).get(
            seeded["clarification_id"]
        )
        assert clarification is not None
        assert clarification.response_payload == {"location": "서울"}
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.RETRIEVING.value
