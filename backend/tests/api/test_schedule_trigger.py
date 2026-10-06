"""API tests for POST /schedules/{id}/trigger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_schedule_runtime import _ensure_tool_execute
from tests.unit.test_schedule_service import (
    _schedule_body,
    _seed_schedule_manager,
    _seed_workflow_target,
)

API = "/api/v1/schedules"


@pytest.fixture
async def trigger_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    from app.auth.passwords import hash_password
    from app.domain.enums import (
        ScheduleMisfirePolicy,
        ScheduleOverlapPolicy,
        ScheduleTargetType,
        ScheduleType,
        UserStatus,
    )
    from app.repositories.user import UserRepository
    from app.services.schedule import ScheduleService

    client = unauthenticated_db_client
    password = "correct-horse-battery-staple"
    async with db_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        _, version_id = await _seed_workflow_target(session, owner_id)
        await _ensure_tool_execute(session, owner_id)
        user = await UserRepository(session).get(owner_id)
        assert user is not None
        await UserRepository(session).set_password_hash(
            user.id, hash_password(password)
        )
        body = _schedule_body(
            target_type=ScheduleTargetType.WORKFLOW_VERSION,
            target_id=version_id,
            schedule_type=ScheduleType.INTERVAL,
            schedule_expression="PT1H",
            timezone="UTC",
            misfire_policy=ScheduleMisfirePolicy.SKIP,
            overlap_policy=ScheduleOverlapPolicy.ALLOW,
        )
        created = await ScheduleService(session).create(body, owner_id=owner_id)
        await session.commit()
        client.trigger_user = user.username  # type: ignore[attr-defined]
        client.trigger_password = password  # type: ignore[attr-defined]
        client.schedule_id = created.id  # type: ignore[attr-defined]

    login = await client.post(
        "/api/v1/auth/login",
        json={
            "username": client.trigger_user,  # type: ignore[attr-defined]
            "password": password,
        },
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


@pytest.mark.asyncio
async def test_trigger_requires_idempotency_key(trigger_client: AsyncClient) -> None:
    schedule_id = trigger_client.schedule_id  # type: ignore[attr-defined]
    resp = await trigger_client.post(f"{API}/{schedule_id}/trigger")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_trigger_creates_schedule_occurrence_execution(
    trigger_client: AsyncClient,
) -> None:
    schedule_id = trigger_client.schedule_id  # type: ignore[attr-defined]
    key = f"trig-{uuid.uuid4()}"
    resp = await trigger_client.post(
        f"{API}/{schedule_id}/trigger",
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["execution_id"] is not None
    assert body["occurrence"]["status"] == "PLANNED"
    assert body["occurrence"]["decision_reason"] == "MANUAL_TRIGGER"
    assert body["occurrence"]["execution_id"] == body["execution_id"]
    assert body["occurrence"]["enqueued_at"] is None

    replay = await trigger_client.post(
        f"{API}/{schedule_id}/trigger",
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert replay.json()["execution_id"] == body["execution_id"]
    assert replay.json()["occurrence"]["id"] == body["occurrence"]["id"]


@pytest.mark.asyncio
async def test_trigger_idempotency_survives_clock_skew(
    trigger_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same Idempotency-Key after clock advances must replay, not IDEMPOTENCY_KEY_REUSED."""
    schedule_id = trigger_client.schedule_id  # type: ignore[attr-defined]
    key = f"trig-clock-{uuid.uuid4()}"
    resp1 = await trigger_client.post(
        f"{API}/{schedule_id}/trigger",
        headers={"Idempotency-Key": key},
    )
    assert resp1.status_code == 201, resp1.text
    body1 = resp1.json()

    # Advance any wall-clock notion by sleeping past a second boundary.
    import asyncio

    await asyncio.sleep(1.1)

    resp2 = await trigger_client.post(
        f"{API}/{schedule_id}/trigger",
        headers={"Idempotency-Key": key},
    )
    assert resp2.status_code == 201, resp2.text
    body2 = resp2.json()
    assert body2["occurrence"]["id"] == body1["occurrence"]["id"]
    assert body2["execution_id"] == body1["execution_id"]

    async with db_session_factory() as session:
        from app.models.execution import Execution
        from sqlalchemy import func, select

        count = (
            await session.execute(
                select(func.count())
                .select_from(Execution)
                .where(Execution.source_type == "SCHEDULE_OCCURRENCE")
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_trigger_other_owner_404(
    trigger_client: AsyncClient,
) -> None:
    resp = await trigger_client.post(
        f"{API}/{uuid.uuid4()}/trigger",
        headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    assert resp.status_code == 404
