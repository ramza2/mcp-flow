"""API tests for /ops/dashboard/summary, execution-stats, system-health."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.domain.enums import (
    ApprovalStatus,
    ExecutionStatus,
    MCPServerStatus,
    MCPToolStatus,
    ScheduleStatus,
)
from app.repositories.user import UserRepository
from tests.helpers.execution_ops import seed_execution, seed_user

OPS = "/api/v1/ops"
PASSWORD = "correct-horse-battery-staple"


async def _operator_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        user_id = await seed_user(session, with_execution_read=True)
        user = await UserRepository(session).get(user_id)
        assert user is not None
        await UserRepository(session).set_password_hash(user.id, hash_password(PASSWORD))
        await session.commit()
        username = user.username
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200
    csrf = await client.get("/api/v1/auth/csrf")
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


@pytest.mark.asyncio
async def test_ops_requires_execution_read(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        user_id = await seed_user(session, with_execution_read=False)
        user = await UserRepository(session).get(user_id)
        assert user is not None
        await UserRepository(session).set_password_hash(user.id, hash_password(PASSWORD))
        await session.commit()
        username = user.username
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200
    forbidden = await client.get(f"{OPS}/dashboard/summary")
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_dashboard_and_stats_window(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = await _operator_client(unauthenticated_db_client, db_session_factory)
    now = datetime(2026, 10, 7, 18, 0, 0, tzinfo=UTC)
    window_from = now - timedelta(hours=24)

    async with db_session_factory() as session:
        user_id = await seed_user(session)
        # Deterministic statuses inside window
        for status, dur in (
            (ExecutionStatus.SUCCEEDED.value, 10),
            (ExecutionStatus.PARTIALLY_SUCCEEDED.value, 20),
            (ExecutionStatus.FAILED.value, 30),
            (ExecutionStatus.CANCELLED.value, 40),
            (ExecutionStatus.TIMED_OUT.value, 50),
            (ExecutionStatus.RUNNING.value, None),
            (ExecutionStatus.WAITING_INPUT.value, None),
            (ExecutionStatus.WAITING_APPROVAL.value, None),
        ):
            started = window_from + timedelta(hours=1)
            finished = (
                started + timedelta(seconds=dur) if dur is not None else None
            )
            await seed_execution(
                session,
                requester_id=user_id,
                status=status,
                requested_at=window_from + timedelta(hours=1),
                started_at=started if status != ExecutionStatus.CREATED.value else None,
                finished_at=finished,
                error_code="TOOL_FAILED" if status == ExecutionStatus.FAILED.value else None,
            )
        # Outside window
        await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.SUCCEEDED.value,
            requested_at=window_from - timedelta(hours=2),
            started_at=window_from - timedelta(hours=2),
            finished_at=window_from - timedelta(hours=1),
        )
        await session.commit()

    # Call service with injected now via repository path — API uses wall clock.
    # Use explicit from/to for deterministic window.
    summary = await client.get(
        f"{OPS}/dashboard/summary",
        params={
            "from": window_from.isoformat().replace("+00:00", "Z"),
            "to": now.isoformat().replace("+00:00", "Z"),
            "recent_limit": 5,
        },
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["executions"]["total"] == 8
    assert body["executions"]["succeeded"] == 1
    assert body["executions"]["partially_succeeded"] == 1
    assert body["executions"]["failed"] == 1
    assert body["executions"]["cancelled"] == 1
    assert body["executions"]["timed_out"] == 1
    assert body["executions"]["running"] == 1
    assert body["executions"]["waiting_input"] == 1
    assert body["executions"]["waiting_approval"] == 1
    assert body["terminal_total"] == 5
    assert body["success_rate"] == pytest.approx(1 / 5)
    # SQLite fallback percentile matches continuous interpolation for 10..50s
    assert body["avg_duration_ms"] == pytest.approx(30_000.0)
    assert body["p95_duration_ms"] == pytest.approx(48_000.0)
    # Aggregate-only — no nested resource IDs/names beyond recent execution list
    assert set(body["approvals"].keys()) == {"pending", "overdue"}
    assert set(body["schedules"].keys()) == {
        "active",
        "paused",
        "completed",
        "error",
        "overdue",
    }
    assert "endpoint_url" not in str(body["mcp_servers"])
    assert len(body["recent_executions"]) <= 5

    stats = await client.get(
        f"{OPS}/execution-stats",
        params={
            "from": window_from.isoformat().replace("+00:00", "Z"),
            "to": now.isoformat().replace("+00:00", "Z"),
        },
    )
    assert stats.status_code == 200, stats.text
    s = stats.json()
    assert s["by_status"]["FAILED"] == 1
    assert s["by_error_category"]["tool"] == 1
    assert s["by_error_category"]["unknown"] == 0
    assert "error_message" not in str(s)
    assert s["duration"]["avg_ms"] == pytest.approx(30_000.0)
    assert s["duration"]["p50_ms"] == pytest.approx(30_000.0)
    assert s["duration"]["p95_ms"] == pytest.approx(48_000.0)
    assert s["duration"]["max_ms"] == pytest.approx(50_000.0)

    health = await client.get(f"{OPS}/system-health")
    assert health.status_code == 200
    h = health.json()
    assert h["database"]["status"] in {"ok", "unavailable"}
    assert "worker" not in h
    assert "redis" not in h
    assert "pending_count" in h["outbox"]
    assert "failed_count" not in h["outbox"]
