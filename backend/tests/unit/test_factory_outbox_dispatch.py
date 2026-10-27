"""Unit tests for durable Factory Outbox dispatch + recovery republish."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.domain.enums import JobStatus, UserStatus
from app.execution.queue import OutboxRelayService, validate_factory_job_dispatch_event
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.models.outbox import OutboxEvent
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.services.factory import FactoryService
from app.services.factory_recovery import FactoryRecoveryService
from app.services.factory_worker import FactoryWorkerService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

MINIMAL_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Demo", "version": "1.0.0"},
        "paths": {
            "/ping": {
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        },
    }
).encode("utf-8")


class RecordingOutboxPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.factory_calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.factory_test_calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.recovery_calls: list[uuid.UUID] = []

    def publish_execution(self, **_kwargs: Any) -> None:
        raise AssertionError("execution unexpected")

    def publish_approval_resume(self, **_kwargs: Any) -> None:
        raise AssertionError("approval unexpected")

    def publish_mrtr_resume(self, **_kwargs: Any) -> None:
        raise AssertionError("mrtr unexpected")

    def publish_factory_job(
        self,
        *,
        job_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        if self.fail:
            raise ConnectionError("broker unavailable")
        self.factory_calls.append((job_id, outbox_event_id))

    def publish_factory_test_run(
        self,
        *,
        test_run_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        if self.fail:
            raise ConnectionError("broker unavailable")
        self.factory_test_calls.append((test_run_id, outbox_event_id))

    def publish_tool_embedding_ensure(self, **_kwargs: Any) -> None:
        raise AssertionError("tool embedding unexpected")

    def publish_factory_recovery(self, *, job_id: uuid.UUID) -> None:
        if self.fail:
            raise ConnectionError("broker unavailable")
        self.recovery_calls.append(job_id)


async def _seed_manage(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fac-ob-{uuid.uuid4().hex[:10]}",
            display_name="Factory Outbox",
            email=f"fac-ob-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fac-ob-r-{uuid.uuid4().hex[:8]}", name="Fac Outbox")
    )
    perm = await PermissionRepository(session).get_by_code("mcp.server.manage")
    assert perm is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[perm.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()
    return user.id


@pytest.mark.asyncio
async def test_create_writes_unpublished_factory_outbox(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_manage(db_session)
    store = InMemoryObjectStorage()
    outcome = await FactoryService(db_session, storage=store).create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == outcome.result.id)
        )
    ).scalar_one()
    assert row.event_type == "FACTORY_JOB_DISPATCH"
    assert row.published_at is None
    assert validate_factory_job_dispatch_event(row) == outcome.result.id


@pytest.mark.asyncio
async def test_outbox_relay_publish_failure_keeps_unpublished_then_recovers(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_manage(db_session)
    store = InMemoryObjectStorage()
    outcome = await FactoryService(db_session, storage=store).create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    job_id = outcome.result.id

    bad = RecordingOutboxPublisher(fail=True)
    failed = await OutboxRelayService(db_session).publish_batch(publisher=bad, limit=10)
    await db_session.commit()
    assert failed.failed == 1
    assert failed.published == 0
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id)
        )
    ).scalar_one()
    assert row.published_at is None
    assert row.last_error_code == "PUBLISH_FAILED"

    good = RecordingOutboxPublisher(fail=False)
    ok = await OutboxRelayService(db_session).publish_batch(publisher=good, limit=10)
    await db_session.commit()
    assert ok.published == 1
    assert good.factory_calls == [(job_id, row.id)]
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id)
        )
    ).scalar_one()
    assert row.published_at is not None

    # Delivered work still completes exactly once via claim fencing.
    worker = FactoryWorkerService(db_session, storage=store, worker_id="u1")
    first = await worker.process_job(job_id=job_id)
    await db_session.commit()
    assert first.status == JobStatus.SUCCEEDED.value
    second = await worker.process_job(job_id=job_id)
    await db_session.commit()
    assert second.claimed is False
    assert second.reason == "TERMINAL"


@pytest.mark.asyncio
async def test_factory_recovery_lists_expired_running_and_stale_queued(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_manage(db_session)
    store = InMemoryObjectStorage()
    svc = FactoryService(db_session, storage=store)
    a = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="a.json",
        idempotency_key=f"k-a-{uuid.uuid4().hex}",
    )
    b = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="b.json",
        idempotency_key=f"k-b-{uuid.uuid4().hex}",
    )
    now = datetime.now(UTC)
    # a → expired RUNNING
    from app.models.factory import ToolFactoryJob

    job_a = await db_session.get(ToolFactoryJob, a.result.id)
    assert job_a is not None
    job_a.status = JobStatus.RUNNING.value
    job_a.worker_id = "dead"
    job_a.lease_token = uuid.uuid4()
    job_a.lease_expires_at = now - timedelta(seconds=5)
    # b → stale QUEUED
    job_b = await db_session.get(ToolFactoryJob, b.result.id)
    assert job_b is not None
    job_b.queued_at = now - timedelta(seconds=120)
    await db_session.commit()

    recovery = FactoryRecoveryService(db_session)
    expired = await recovery.list_expired_running_ids(limit=10, now=now)
    stale = await recovery.list_stale_queued_ids(
        limit=10, min_age_seconds=30, now=now
    )
    assert a.result.id in expired
    assert b.result.id in stale
    assert b.result.id not in expired
