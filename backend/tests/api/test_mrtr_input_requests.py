"""API tests for MRTR Input routes (docs/06 §15)."""

from __future__ import annotations

import json
import uuid

import pytest
from app.domain.enums import ExecutionStatus, McpInputRequestStatus
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from app.repositories.user import UserRepository
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.test_auth_session import PASSWORD
from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_mrtr_waiting_input import _CANARY, _MrtrClient, _runner
from tests.unit.test_tool_runner import _claim_ready_execution


async def _login_requester(
    client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
) -> None:
    async with db_session_factory() as session:
        user = await UserRepository(session).get(user_id)
        assert user is not None
        # Ensure password matches test fixture.
        from app.auth.passwords import hash_password

        await UserRepository(session).set_password_hash(
            user_id, hash_password(PASSWORD)
        )
        await session.commit()
        username = user.username
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]


async def _waiting_execution(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        db_session_factory
    )
    outcome = await _runner(db_session_factory, _MrtrClient()).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == "WAITING_INPUT"
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        mir = (
            await MCPInputRequestRepository(session).list_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )[0]
        return execution_id, mir.id, execution.requester_id


@pytest.mark.asyncio
async def test_list_and_respond_hides_request_state(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, requester = await _waiting_execution(
        db_session_factory, monkeypatch
    )
    await _login_requester(db_client, db_session_factory, user_id=requester)

    listed = await db_client.get(
        f"/api/v1/executions/{execution_id}/input-requests",
        params={"status": "OPEN"},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == str(mir_id)
    assert item["status"] == McpInputRequestStatus.OPEN.value
    assert "requestState" not in item
    assert "request_state" not in item
    assert "response_payload" not in item
    blob = json.dumps(item)
    assert _CANARY not in blob

    detail = await db_client.get(
        f"/api/v1/executions/{execution_id}/input-requests/{mir_id}"
    )
    assert detail.status_code == 200
    assert "requestState" not in detail.json()
    assert _CANARY not in json.dumps(detail.json())

    responded = await db_client.post(
        f"/api/v1/executions/{execution_id}/input-requests/{mir_id}/responses",
        json={"responses": {"city": "Seoul", "units": "c"}},
    )
    assert responded.status_code == 201, responded.text
    data = responded.json()
    assert data["status"] == McpInputRequestStatus.ANSWERED.value
    assert data["resume_enqueued"] is True
    assert data["execution_status"] == ExecutionStatus.WAITING_INPUT.value
    assert _CANARY not in json.dumps(data)

    async with db_session_factory() as session:
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_MRTR_RESUME"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        mir = await MCPInputRequestRepository(session).get(mir_id)
        assert mir is not None
        assert mir.request_state["token"] == _CANARY


@pytest.mark.asyncio
async def test_reject_via_api(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, requester = await _waiting_execution(
        db_session_factory, monkeypatch
    )
    await _login_requester(db_client, db_session_factory, user_id=requester)

    rejected = await db_client.post(
        f"/api/v1/executions/{execution_id}/input-requests/{mir_id}/reject"
    )
    assert rejected.status_code == 200, rejected.text
    data = rejected.json()
    assert data["status"] == McpInputRequestStatus.REJECTED.value
    assert data["execution_status"] == ExecutionStatus.FAILED.value

    async with db_session_factory() as session:
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_MRTR_RESUME"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert events == []
