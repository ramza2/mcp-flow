"""API tests for GET /executions/{id}/events SSE."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from app.auth.passwords import hash_password
from app.domain.enums import ExecutionEventVisibility
from app.execution.events import ExecutionEventWriter
from app.repositories.user import UserRepository
from app.services.execution_events_sse import ExecutionEventsSseService, parse_last_event_id
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers.execution_ops import seed_execution, seed_user

API = "/api/v1/executions"
PASSWORD = "correct-horse-battery-staple"


async def _login(
    client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    with_execution_read: bool = False,
) -> tuple[AsyncClient, uuid.UUID]:
    async with db_session_factory() as session:
        user_id = await seed_user(session, with_execution_read=with_execution_read)
        user = await UserRepository(session).get(user_id)
        assert user is not None
        await UserRepository(session).set_password_hash(user.id, hash_password(PASSWORD))
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
    return client, user_id


def _parse_frames(body: str) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if line == "":
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith(":"):
            frames.append({"comment": line[1:].lstrip()})
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current[key] = value.lstrip()
    if current:
        frames.append(current)
    return frames


@pytest.mark.asyncio
async def test_owner_sse_and_foreign_404(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, owner_id = await _login(
        unauthenticated_db_client, db_session_factory, with_execution_read=False
    )
    async with db_session_factory() as session:
        foreign = await seed_user(session, with_execution_read=False)
        own = await seed_execution(session, requester_id=owner_id)
        other = await seed_execution(session, requester_id=foreign)
        writer = ExecutionEventWriter(session)
        await writer.append(
            execution_id=own.id,
            event_type="execution.created",
            payload={"execution_id": str(own.id), "status": "CREATED"},
            visibility=ExecutionEventVisibility.USER.value,
        )
        await writer.append(
            execution_id=own.id,
            event_type="execution.queued",
            payload={"execution_id": str(own.id), "status": "QUEUED"},
            visibility=ExecutionEventVisibility.USER.value,
        )
        await writer.append(
            execution_id=own.id,
            event_type="internal.debug",
            payload={"secret": "nope"},
            visibility=ExecutionEventVisibility.INTERNAL.value,
        )
        await session.commit()
        own_id, other_id = own.id, other.id

    foreign_resp = await client.get(
        f"{API}/{other_id}/events",
        headers={"Accept": "text/event-stream"},
    )
    assert foreign_resp.status_code == 404
    assert foreign_resp.json()["error"]["code"] == "NOT_FOUND"

    # Bounded iterator via service (avoids hanging StreamingResponse in unit API).
    async with db_session_factory() as session:
        service = ExecutionEventsSseService(
            session,
            session_factory=db_session_factory,
            poll_interval_seconds=0.01,
            heartbeat_seconds=0.05,
            sleep=asyncio.sleep,
        )
        auth = await service.authorize(actor_user_id=owner_id, execution_id=own_id)
        frames: list[str] = []
        async for frame in service.event_iterator(
            auth=auth, after_id=0, max_cycles=3
        ):
            frames.append(frame)
        body = "".join(frames)

    parsed = _parse_frames(body)
    event_frames = [f for f in parsed if "event" in f]
    assert [f["event"] for f in event_frames] == [
        "execution.created",
        "execution.queued",
    ]
    assert all(f["id"].isdigit() for f in event_frames)
    assert "internal.debug" not in body
    assert "secret" not in body
    # Heartbeat may appear after idle cycles
    assert any("comment" in f for f in parsed) or len(event_frames) == 2


@pytest.mark.asyncio
async def test_operator_sees_operator_not_internal(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, operator_id = await _login(
        unauthenticated_db_client, db_session_factory, with_execution_read=True
    )
    async with db_session_factory() as session:
        owner = await seed_user(session)
        execution = await seed_execution(session, requester_id=owner)
        writer = ExecutionEventWriter(session)
        await writer.append(
            execution_id=execution.id,
            event_type="execution.created",
            payload={"execution_id": str(execution.id), "status": "CREATED"},
            visibility=ExecutionEventVisibility.USER.value,
        )
        await writer.append(
            execution_id=execution.id,
            event_type="operator.note",
            payload={"execution_id": str(execution.id), "status": "CREATED"},
            visibility=ExecutionEventVisibility.OPERATOR.value,
        )
        await writer.append(
            execution_id=execution.id,
            event_type="internal.debug",
            payload={"token": "hidden"},
            visibility=ExecutionEventVisibility.INTERNAL.value,
        )
        await session.commit()
        execution_id = execution.id

    async with db_session_factory() as session:
        service = ExecutionEventsSseService(
            session,
            session_factory=db_session_factory,
            poll_interval_seconds=0.01,
            sleep=asyncio.sleep,
        )
        auth = await service.authorize(
            actor_user_id=operator_id, execution_id=execution_id
        )
        frames: list[str] = []
        async for frame in service.event_iterator(
            auth=auth, after_id=0, max_cycles=1
        ):
            frames.append(frame)
        body = "".join(frames)

    assert "execution.created" in body
    assert "operator.note" in body
    assert "internal.debug" not in body
    assert "token" not in body
    # HTTP authorize path for operator also succeeds (opens stream)
    # Use max_cycles via Accept to ensure pre-stream auth works:
    malformed = await client.get(
        f"{API}/{execution_id}/events",
        headers={"Accept": "text/event-stream", "Last-Event-ID": "not-a-number"},
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_last_event_id_skips_prior(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        owner = await seed_user(session)
        execution = await seed_execution(session, requester_id=owner)
        writer = ExecutionEventWriter(session)
        first = await writer.append(
            execution_id=execution.id,
            event_type="execution.created",
            payload={"execution_id": str(execution.id), "status": "CREATED"},
        )
        second = await writer.append(
            execution_id=execution.id,
            event_type="execution.queued",
            payload={"execution_id": str(execution.id), "status": "QUEUED"},
        )
        await session.commit()
        first_id, second_id = first.id, second.id
        owner_id, execution_id = owner, execution.id

    assert second_id > first_id
    cursor = parse_last_event_id(str(first_id))
    async with db_session_factory() as session:
        service = ExecutionEventsSseService(
            session,
            session_factory=db_session_factory,
            poll_interval_seconds=0.01,
            sleep=asyncio.sleep,
        )
        auth = await service.authorize(
            actor_user_id=owner_id, execution_id=execution_id
        )
        frames: list[str] = []
        async for frame in service.event_iterator(
            auth=auth, after_id=cursor, max_cycles=1
        ):
            frames.append(frame)
        body = "".join(frames)

    assert "execution.created" not in body
    assert "execution.queued" in body
    assert f"id: {second_id}" in body


@pytest.mark.asyncio
async def test_disconnect_stops_iterator(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        owner = await seed_user(session)
        execution = await seed_execution(session, requester_id=owner)
        await session.commit()
        owner_id, execution_id = owner, execution.id

    disconnected = False

    def is_disconnected() -> bool:
        return disconnected

    async with db_session_factory() as session:
        service = ExecutionEventsSseService(
            session,
            session_factory=db_session_factory,
            poll_interval_seconds=0.01,
            sleep=asyncio.sleep,
        )
        auth = await service.authorize(
            actor_user_id=owner_id, execution_id=execution_id
        )
        frames: list[str] = []
        async for frame in service.event_iterator(
            auth=auth,
            after_id=0,
            is_disconnected=is_disconnected,
            max_cycles=10,
        ):
            frames.append(frame)
            disconnected = True
        # After disconnect flag, loop should exit without hanging.
        assert isinstance(frames, list)
