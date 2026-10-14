"""Unit tests for Factory sandbox TestRun create + worker (Issue #91)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.canonical_hash import encode_canonical_json_bytes
from app.core.errors import AppError
from app.domain.enums import JobStatus, UserStatus
from app.factory import (
    FACTORY_CONTRACT_POLICY_VERSION,
    FACTORY_SECURITY_POLICY_VERSION,
)
from app.factory.errors import FACTORY_SOURCE_OBJECT_MISSING
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.models.factory import ToolFactoryGeneratedPackage, ToolFactoryTestRun
from app.models.mcp import MCPServer, MCPTool
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
from app.services.factory_test_worker import FactoryTestWorkerService
from app.services.factory_worker import FactoryWorkerService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

MINIMAL_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Demo", "version": "1.0.0"},
        "paths": {
            "/ping": {
                "get": {
                    "operationId": "ping",
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/pong": {
                "post": {
                    "operationId": "pong",
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    },
    separators=(",", ":"),
).encode("utf-8")


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fac-tr-{uuid.uuid4().hex[:10]}",
            display_name="Factory TestRun",
            email=f"fac-tr-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fac-tr-r-{uuid.uuid4().hex[:8]}", name="Fac TestRun")
    )
    manage = await PermissionRepository(session).get_by_code("mcp.server.manage")
    read = await PermissionRepository(session).get_by_code("mcp.server.read")
    assert manage is not None and read is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[manage.id, read.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()
    return user.id


async def _package_ready(
    session: AsyncSession,
) -> tuple[FactoryService, InMemoryObjectStorage, uuid.UUID, uuid.UUID]:
    user_id = await _seed_user(session)
    store = InMemoryObjectStorage()
    svc = FactoryService(session, storage=store)
    job = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="pets.json",
        idempotency_key=f"job-{uuid.uuid4().hex}",
    )
    worker = FactoryWorkerService(session, storage=store, worker_id="u-analyze")
    result = await worker.process_job(job_id=job.result.id)
    await session.commit()
    assert result.status == JobStatus.SUCCEEDED.value
    cands = await svc.list_candidates_for_job(user_id, job.result.id)
    build = await svc.create_build(
        actor_user_id=user_id,
        job_id=job.result.id,
        candidate_ids=[item.id for item in cands.items],
        idempotency_key=f"build-{uuid.uuid4().hex}",
    )
    pkg = await svc.generate_package(
        actor_user_id=user_id,
        build_id=build.result.id,
        idempotency_key=f"pkg-{uuid.uuid4().hex}",
    )
    return svc, store, user_id, pkg.result.id


@pytest.mark.asyncio
async def test_create_test_run_outbox_and_worker_pass(
    db_session: AsyncSession,
) -> None:
    svc, store, user_id, package_id = await _package_ready(db_session)
    outcome = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=package_id,
        idempotency_key=f"tr-{uuid.uuid4().hex}",
    )
    assert outcome.http_status == 201
    assert outcome.replayed is False
    assert outcome.result.status == JobStatus.QUEUED
    assert outcome.result.security_policy_version == FACTORY_SECURITY_POLICY_VERSION
    assert outcome.result.contract_policy_version == FACTORY_CONTRACT_POLICY_VERSION
    assert outcome.result.result is None

    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == outcome.result.id)
        )
    ).scalar_one()
    assert row.event_type == "FACTORY_TEST_RUN_DISPATCH"
    assert row.published_at is None
    assert row.payload == {"test_run_id": str(outcome.result.id)}

    worker = FactoryTestWorkerService(db_session, storage=store, worker_id="u-test")
    first = await worker.process_test_run(test_run_id=outcome.result.id)
    await db_session.commit()
    assert first.claimed is True
    assert first.status == JobStatus.SUCCEEDED.value

    detail = await svc.get_test_run(user_id, outcome.result.id)
    assert detail.status == JobStatus.SUCCEEDED
    assert detail.result == "PASS"
    assert detail.evidence_hash is not None
    assert len(detail.evidence_hash) == 64
    assert detail.security_result is not None
    assert detail.security_result["passed"] is True
    assert detail.contract_result is not None
    assert detail.contract_result["passed"] is True
    assert detail.error_code is None
    # Safe projection: raw evidence blob / OS keys are not exposed.
    dumped = detail.model_dump(mode="json")
    assert "evidence" not in dumped or dumped.get("evidence") is None
    assert "object_key" not in dumped
    assert "object_bucket" not in dumped

    # Duplicate delivery is no-op.
    second = await worker.process_test_run(test_run_id=outcome.result.id)
    await db_session.commit()
    assert second.claimed is False
    assert second.reason == "TERMINAL"

    tools = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    servers = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    assert int(tools) == 0
    assert int(servers) == 0

    listed = await svc.list_test_runs_for_package(user_id, package_id)
    assert listed.total == 1
    assert listed.items[0].id == outcome.result.id


@pytest.mark.asyncio
async def test_test_run_idempotency_replay_and_natural_unique(
    db_session: AsyncSession,
) -> None:
    svc, _store, user_id, package_id = await _package_ready(db_session)
    key = f"tr-idem-{uuid.uuid4().hex}"
    first = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=package_id,
        idempotency_key=key,
    )
    second = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=package_id,
        idempotency_key=key,
    )
    assert second.replayed is True
    assert second.result.id == first.result.id

    # Different key but same package+policies returns the natural unique row.
    third = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=package_id,
        idempotency_key=f"tr-other-{uuid.uuid4().hex}",
    )
    assert third.result.id == first.result.id
    count = (
        await db_session.execute(
            select(func.count()).select_from(ToolFactoryTestRun).where(
                ToolFactoryTestRun.generated_package_id == package_id
            )
        )
    ).scalar_one()
    assert int(count) == 1


@pytest.mark.asyncio
async def test_worker_fail_closed_missing_corrupt_hash_lineage(
    db_session: AsyncSession,
) -> None:
    svc, store, user_id, package_id = await _package_ready(db_session)
    package_row = await db_session.get(ToolFactoryGeneratedPackage, package_id)
    assert package_row is not None

    # Missing object after enqueue.
    missing = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=package_id,
        idempotency_key=f"tr-miss-{uuid.uuid4().hex}",
    )
    await store.delete_object(
        bucket=package_row.object_bucket,
        object_key=package_row.object_key,
    )
    worker = FactoryTestWorkerService(db_session, storage=store, worker_id="u-miss")
    miss_out = await worker.process_test_run(test_run_id=missing.result.id)
    await db_session.commit()
    assert miss_out.status == JobStatus.FAILED.value
    miss_detail = await svc.get_test_run(user_id, missing.result.id)
    assert miss_detail.result == "FAIL"
    assert miss_detail.error_code == FACTORY_SOURCE_OBJECT_MISSING
    assert miss_detail.evidence_hash is not None

    # New package for remaining cases (unique policies consumed above).
    svc2, store2, user_id2, package_id2 = await _package_ready(db_session)
    package_row2 = await db_session.get(ToolFactoryGeneratedPackage, package_id2)
    assert package_row2 is not None
    corrupt = await svc2.create_test_run(
        actor_user_id=user_id2,
        package_id=package_id2,
        idempotency_key=f"tr-corr-{uuid.uuid4().hex}",
    )
    await store2.put_bytes(
        bucket=package_row2.object_bucket,
        object_key=package_row2.object_key,
        data=b"{not-json",
        content_type="application/json",
    )
    # Size may still mismatch; worker treats decode failure as corrupt.
    corr_out = await FactoryTestWorkerService(
        db_session, storage=store2, worker_id="u-corr"
    ).process_test_run(test_run_id=corrupt.result.id)
    await db_session.commit()
    assert corr_out.status == JobStatus.FAILED.value
    corr_detail = await svc2.get_test_run(user_id2, corrupt.result.id)
    assert corr_detail.result == "FAIL"
    assert corr_detail.error_code in {
        "FACTORY_PACKAGE_SCHEMA_INVALID",
        "FACTORY_PACKAGE_SIZE_MISMATCH",
    }

    # Hash mismatch with valid JSON of matching size is hard; pad/truncate to
    # keep size while changing content → size or hash fail.
    svc3, store3, user_id3, package_id3 = await _package_ready(db_session)
    package_row3 = await db_session.get(ToolFactoryGeneratedPackage, package_id3)
    assert package_row3 is not None
    hash_run = await svc3.create_test_run(
        actor_user_id=user_id3,
        package_id=package_id3,
        idempotency_key=f"tr-hash-{uuid.uuid4().hex}",
    )
    original = await store3.get_bytes(
        bucket=package_row3.object_bucket,
        object_key=package_row3.object_key,
    )
    payload = json.loads(original.decode("utf-8"))
    payload["tools"][0]["summary"] = "tampered-summary"
    tampered = encode_canonical_json_bytes(payload)
    # If size differs, pad with spaces inside a string field already done via
    # canonical re-encode; size will typically change → size or hash fail-closed.
    await store3.put_bytes(
        bucket=package_row3.object_bucket,
        object_key=package_row3.object_key,
        data=tampered,
        content_type="application/json",
    )
    hash_out = await FactoryTestWorkerService(
        db_session, storage=store3, worker_id="u-hash"
    ).process_test_run(test_run_id=hash_run.result.id)
    await db_session.commit()
    assert hash_out.status == JobStatus.FAILED.value
    hash_detail = await svc3.get_test_run(user_id3, hash_run.result.id)
    assert hash_detail.result == "FAIL"
    assert hash_detail.error_code in {
        "FACTORY_PACKAGE_HASH_MISMATCH",
        "FACTORY_PACKAGE_SIZE_MISMATCH",
    }

    # Lineage mismatch: mutate TestRun snapshot after enqueue.
    svc4, store4, user_id4, package_id4 = await _package_ready(db_session)
    lineage = await svc4.create_test_run(
        actor_user_id=user_id4,
        package_id=package_id4,
        idempotency_key=f"tr-lin-{uuid.uuid4().hex}",
    )
    run_row = await db_session.get(ToolFactoryTestRun, lineage.result.id)
    assert run_row is not None
    run_row.package_content_sha256 = "f" * 64
    await db_session.commit()
    lin_out = await FactoryTestWorkerService(
        db_session, storage=store4, worker_id="u-lin"
    ).process_test_run(test_run_id=lineage.result.id)
    await db_session.commit()
    assert lin_out.status == JobStatus.FAILED.value
    lin_detail = await svc4.get_test_run(user_id4, lineage.result.id)
    assert lin_detail.error_code == "FACTORY_PACKAGE_LINEAGE_MISMATCH"


@pytest.mark.asyncio
async def test_test_run_recovery_lists_expired_and_stale(
    db_session: AsyncSession,
) -> None:
    svc, _store, user_id, package_id = await _package_ready(db_session)
    a = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=package_id,
        idempotency_key=f"tr-a-{uuid.uuid4().hex}",
    )
    # Need a second package for a second TestRun (unique policies).
    svc2, _store2, user_id2, package_id2 = await _package_ready(db_session)
    b = await svc2.create_test_run(
        actor_user_id=user_id2,
        package_id=package_id2,
        idempotency_key=f"tr-b-{uuid.uuid4().hex}",
    )
    now = datetime.now(UTC)
    row_a = await db_session.get(ToolFactoryTestRun, a.result.id)
    assert row_a is not None
    row_a.status = JobStatus.RUNNING.value
    row_a.worker_id = "dead"
    row_a.lease_token = uuid.uuid4()
    row_a.lease_expires_at = now - timedelta(seconds=5)
    row_b = await db_session.get(ToolFactoryTestRun, b.result.id)
    assert row_b is not None
    row_b.queued_at = now - timedelta(seconds=120)
    await db_session.commit()

    recovery = FactoryRecoveryService(db_session)
    expired = await recovery.list_expired_running_test_run_ids(limit=10, now=now)
    stale = await recovery.list_stale_queued_test_run_ids(
        limit=10, min_age_seconds=30, now=now
    )
    assert a.result.id in expired
    assert b.result.id in stale
    assert b.result.id not in expired


@pytest.mark.asyncio
async def test_create_test_run_rejects_missing_package(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    svc = FactoryService(db_session, storage=InMemoryObjectStorage())
    with pytest.raises(AppError) as exc:
        await svc.create_test_run(
            actor_user_id=user_id,
            package_id=uuid.uuid4(),
            idempotency_key=f"tr-{uuid.uuid4().hex}",
        )
    assert exc.value.status_code == 404
