"""PostgreSQL concurrency / persistence evidence for async Tool Factory."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import JobStatus, UserStatus
from app.factory import ARTIFACT_TYPE_OPENAPI_ANALYSIS, JOB_TYPE_OPENAPI_ANALYZE
from app.factory.errors import (
    FACTORY_SOURCE_OBJECT_CORRUPT,
    FACTORY_SOURCE_OBJECT_MISSING,
)
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.services.factory import FactoryService
from app.services.factory_worker import FactoryWorkerService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_OPERATION_SCOPE = "FACTORY_OPENAPI_ANALYZE_V1"
_RESOURCE_TYPE = "TOOL_FACTORY_JOB"

MINIMAL_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Demo", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ping": {
                "get": {
                    "operationId": "ping",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    },
    separators=(",", ":"),
).encode("utf-8")

OTHER_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Other", "version": "2.0.0"},
        "servers": [{"url": "https://other.example.com"}],
        "paths": {
            "/pong": {
                "get": {
                    "operationId": "pong",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    },
    separators=(",", ":"),
).encode("utf-8")


async def _seed_manage_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fac-pg-{uuid.uuid4().hex[:10]}",
            display_name="Factory PG Tester",
            email=f"fac-pg-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fac-pg-r-{uuid.uuid4().hex[:8]}", name="Factory PG Role")
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


def _install_create_completed_barrier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parties: int = 2,
) -> asyncio.Barrier:
    barrier = asyncio.Barrier(parties)
    original = IdempotencyRepository.create_completed

    async def _synced(self: IdempotencyRepository, **kwargs: Any) -> Any:
        await barrier.wait()
        return await original(self, **kwargs)

    monkeypatch.setattr(IdempotencyRepository, "create_completed", _synced)
    return barrier


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_same_source_idempotency_one_job(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)

    key = f"fac-race-{uuid.uuid4().hex}"
    filename = "race.json"
    storage = InMemoryObjectStorage()
    _install_create_completed_barrier(monkeypatch, parties=2)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await FactoryService(
                    session, storage=storage
                ).create_openapi_analyze_job(
                    actor_user_id=user_id,
                    source_bytes=MINIMAL_JSON,
                    filename=filename,
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    outcomes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert not errors, f"unexpected AppError in same-source race: {errors}"
    assert len(outcomes) == 2
    replay_flags = sorted(o.replayed for o in outcomes)
    assert replay_flags == [False, True]
    job_ids = {o.result.id for o in outcomes}
    assert len(job_ids) == 1
    surviving_id = next(iter(job_ids))
    # Exactly one durable source object remains (loser deleted orphan upload).
    assert storage.object_count() == 1

    async with integration_session_factory() as session:
        job_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM tool_factory_jobs
                    WHERE requested_by = :uid
                      AND job_type = :jt
                      AND source_name = :name
                    """
                ),
                {
                    "uid": str(user_id),
                    "jt": JOB_TYPE_OPENAPI_ANALYZE,
                    "name": filename,
                },
            )
        ).scalar_one()
        assert int(job_count) == 1

        row = (
            await session.execute(
                text(
                    """
                    SELECT status, source_bucket, source_object_key, source_size_bytes
                    FROM tool_factory_jobs WHERE id = :jid
                    """
                ),
                {"jid": str(surviving_id)},
            )
        ).one()
        assert row[0] == JobStatus.QUEUED.value
        assert row[1] and row[2]
        assert int(row[3]) == len(MINIMAL_JSON)

        idemp_rows = (
            await session.execute(
                text(
                    """
                    SELECT resource_type, resource_id::text, operation_scope
                    FROM api_idempotency_records
                    WHERE principal_key = :pk
                      AND operation_scope = :scope
                      AND idempotency_key = :key
                    """
                ),
                {
                    "pk": str(user_id),
                    "scope": _OPERATION_SCOPE,
                    "key": key,
                },
            )
        ).all()
        assert len(idemp_rows) == 1
        assert idemp_rows[0][0] == _RESOURCE_TYPE
        assert idemp_rows[0][1] == str(surviving_id)

        outbox_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM outbox_events
                    WHERE event_type = 'FACTORY_JOB_DISPATCH'
                      AND aggregate_id = :jid
                    """
                ),
                {"jid": str(surviving_id)},
            )
        ).scalar_one()
        assert int(outbox_count) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_same_key_different_source_one_job(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)

    key = f"fac-diff-{uuid.uuid4().hex}"
    filename = "race.json"
    storage = InMemoryObjectStorage()
    _install_create_completed_barrier(monkeypatch, parties=2)

    async def _run(source: bytes) -> Any:
        async with integration_session_factory() as session:
            try:
                return await FactoryService(
                    session, storage=storage
                ).create_openapi_analyze_job(
                    actor_user_id=user_id,
                    source_bytes=source,
                    filename=filename,
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(MINIMAL_JSON), _run(OTHER_JSON))
    outcomes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert len(outcomes) == 1
    assert outcomes[0].replayed is False
    assert len(errors) == 1
    assert errors[0].code == "IDEMPOTENCY_KEY_REUSED"
    assert storage.object_count() == 1

    async with integration_session_factory() as session:
        job_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tool_factory_jobs WHERE requested_by = :uid"
                ),
                {"uid": str(user_id)},
            )
        ).scalar_one()
        assert int(job_count) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_worker_restart_reconstructs_from_object(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = InMemoryObjectStorage()
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)
        outcome = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="restart.json",
            idempotency_key=f"k-{uuid.uuid4().hex}",
        )
        await session.execute(
            text(
                """
                UPDATE tool_factory_jobs
                SET status = 'RUNNING',
                    worker_id = 'dead',
                    lease_token = :tok,
                    lease_expires_at = :exp
                WHERE id = :jid
                """
            ),
            {
                "tok": str(uuid.uuid4()),
                "exp": datetime.now(UTC) - timedelta(seconds=60),
                "jid": str(outcome.result.id),
            },
        )
        await session.commit()
        job_id = outcome.result.id

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(
            session, storage=storage, worker_id="pg-restart"
        )
        result = await worker.process_job(job_id=job_id)
        await session.commit()
        assert result.claimed is True
        assert result.status == JobStatus.SUCCEEDED.value

    async with integration_session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT status, operation_count FROM tool_factory_jobs WHERE id = :jid
                    """
                ),
                {"jid": str(job_id)},
            )
        ).one()
        assert row[0] == JobStatus.SUCCEEDED.value
        assert int(row[1]) == 1
        art = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM tool_factory_artifacts
                    WHERE job_id = :jid AND artifact_type = :atype
                    """
                ),
                {"jid": str(job_id), "atype": ARTIFACT_TYPE_OPENAPI_ANALYSIS},
            )
        ).scalar_one()
        assert int(art) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_missing_and_corrupt_object_fail_job(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = InMemoryObjectStorage()
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)
        missing = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="missing.json",
            idempotency_key=f"k-m-{uuid.uuid4().hex}",
        )
        corrupt = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="corrupt.json",
            idempotency_key=f"k-c-{uuid.uuid4().hex}",
        )
        miss_row = (
            await session.execute(
                text(
                    "SELECT source_bucket, source_object_key FROM tool_factory_jobs "
                    "WHERE id = :jid"
                ),
                {"jid": str(missing.result.id)},
            )
        ).one()
        await storage.delete_object(bucket=miss_row[0], object_key=miss_row[1])
        corr_row = (
            await session.execute(
                text(
                    "SELECT source_bucket, source_object_key FROM tool_factory_jobs "
                    "WHERE id = :jid"
                ),
                {"jid": str(corrupt.result.id)},
            )
        ).one()
        await storage.put_bytes(
            bucket=corr_row[0],
            object_key=corr_row[1],
            data=OTHER_JSON,
            content_type="application/json",
        )
        await session.commit()
        missing_id = missing.result.id
        corrupt_id = corrupt.result.id

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-err")
        m = await worker.process_job(job_id=missing_id)
        await session.commit()
        assert m.status == JobStatus.FAILED.value
        assert m.reason == FACTORY_SOURCE_OBJECT_MISSING

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-err")
        c = await worker.process_job(job_id=corrupt_id)
        await session.commit()
        assert c.status == JobStatus.FAILED.value
        assert c.reason == FACTORY_SOURCE_OBJECT_CORRUPT

    async with integration_session_factory() as session:
        codes = (
            await session.execute(
                text(
                    "SELECT error_code FROM tool_factory_jobs "
                    "WHERE id IN (:a, :b) ORDER BY source_name"
                ),
                {"a": str(corrupt_id), "b": str(missing_id)},
            )
        ).scalars().all()
        assert set(codes) == {
            FACTORY_SOURCE_OBJECT_CORRUPT,
            FACTORY_SOURCE_OBJECT_MISSING,
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_outbox_publish_failure_then_recovery_one_analysis(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Broker publish failure leaves unpublished Outbox; relay + worker → one outcome."""

    from app.execution.queue import OutboxRelayService

    class _Pub:
        def __init__(self) -> None:
            self.fail_once = True
            self.calls: list[uuid.UUID] = []

        def publish_execution(self, **_k: Any) -> None:
            raise AssertionError("unexpected")

        def publish_approval_resume(self, **_k: Any) -> None:
            raise AssertionError("unexpected")

        def publish_mrtr_resume(self, **_k: Any) -> None:
            raise AssertionError("unexpected")

        def publish_factory_job(
            self, *, job_id: uuid.UUID, outbox_event_id: uuid.UUID
        ) -> None:
            del outbox_event_id
            if self.fail_once:
                self.fail_once = False
                raise ConnectionError("broker down")
            self.calls.append(job_id)

        def publish_factory_test_run(self, **_k: Any) -> None:
            raise AssertionError("unexpected test-run publish")

    storage = InMemoryObjectStorage()
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)
        outcome = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="recover.json",
            idempotency_key=f"k-{uuid.uuid4().hex}",
        )
        job_id = outcome.result.id
        await session.commit()

    pub = _Pub()
    async with integration_session_factory() as session:
        first = await OutboxRelayService(session).publish_batch(publisher=pub, limit=10)
        await session.commit()
        assert first.failed == 1
        assert first.published == 0
        unpublished = (
            await session.execute(
                text(
                    "SELECT published_at IS NULL, last_error_code "
                    "FROM outbox_events WHERE aggregate_id = :jid "
                    "AND event_type = 'FACTORY_JOB_DISPATCH'"
                ),
                {"jid": str(job_id)},
            )
        ).one()
        assert unpublished[0] is True
        assert unpublished[1] == "PUBLISH_FAILED"

    async with integration_session_factory() as session:
        second = await OutboxRelayService(session).publish_batch(publisher=pub, limit=10)
        await session.commit()
        assert second.published == 1
        assert pub.calls == [job_id]

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-ob")
        result = await worker.process_job(job_id=job_id)
        await session.commit()
        assert result.claimed is True
        assert result.status == JobStatus.SUCCEEDED.value

    async with integration_session_factory() as session:
        # Duplicate delivery / recovery republish is a no-op after terminal.
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-ob-2")
        again = await worker.process_job(job_id=job_id)
        await session.commit()
        assert again.claimed is False
        art = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tool_factory_artifacts "
                    "WHERE job_id = :jid AND artifact_type = :atype"
                ),
                {"jid": str(job_id), "atype": ARTIFACT_TYPE_OPENAPI_ANALYSIS},
            )
        ).scalar_one()
        assert int(art) == 1
        jobs = (
            await session.execute(
                text("SELECT count(*) FROM tool_factory_jobs WHERE id = :jid"),
                {"jid": str(job_id)},
            )
        ).scalar_one()
        assert int(jobs) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_candidate_materialize_no_duplicates(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two sessions materializing the same SUCCEEDED analysis keep one row/key."""
    storage = InMemoryObjectStorage()
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)
        outcome = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="cand.json",
            idempotency_key=f"cand-{uuid.uuid4().hex}",
        )
        await session.commit()
        job_id = outcome.result.id

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-cand")
        result = await worker.process_job(job_id=job_id)
        await session.commit()
        assert result.status == JobStatus.SUCCEEDED.value

    # Wipe candidates to simulate a race where both sessions see empty keys.
    async with integration_session_factory() as session:
        await session.execute(
            text("DELETE FROM tool_factory_candidates WHERE job_id = :jid"),
            {"jid": str(job_id)},
        )
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def _materialize() -> int:
        async with integration_session_factory() as session:
            from app.factory.contracts import FactoryOpenAPIAnalysis
            from app.models.factory import ToolFactoryArtifact, ToolFactoryJob
            from sqlalchemy import select

            job = await session.get(ToolFactoryJob, job_id)
            assert job is not None
            artifact = (
                await session.execute(
                    select(ToolFactoryArtifact).where(
                        ToolFactoryArtifact.job_id == job_id
                    )
                )
            ).scalar_one()
            analysis = FactoryOpenAPIAnalysis.model_validate(artifact.inline_payload)
            await barrier.wait()
            created = await FactoryService(
                session, storage=storage
            ).materialize_candidates_from_analysis(
                job=job,
                artifact=artifact,
                analysis=analysis,
            )
            await session.commit()
            return created

    created_counts = await asyncio.gather(_materialize(), _materialize())
    assert sum(created_counts) >= 1

    async with integration_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tool_factory_candidates WHERE job_id = :jid"
                ),
                {"jid": str(job_id)},
            )
        ).scalar_one()
        assert int(count) == 1
        keys = (
            await session.execute(
                text(
                    "SELECT operation_key FROM tool_factory_candidates "
                    "WHERE job_id = :jid"
                ),
                {"jid": str(job_id)},
            )
        ).scalars().all()
        assert list(keys) == ["GET /ping"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_build_create_same_idempotency_key(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same Idempotency-Key concurrent Build create keeps a single Build."""
    storage = InMemoryObjectStorage()
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)
        outcome = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="build.json",
            idempotency_key=f"job-{uuid.uuid4().hex}",
        )
        await session.commit()
        job_id = outcome.result.id

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-build")
        result = await worker.process_job(job_id=job_id)
        await session.commit()
        assert result.status == JobStatus.SUCCEEDED.value
        cand_id = (
            await session.execute(
                text(
                    "SELECT id FROM tool_factory_candidates WHERE job_id = :jid LIMIT 1"
                ),
                {"jid": str(job_id)},
            )
        ).scalar_one()

    key = f"build-race-{uuid.uuid4().hex}"
    _install_create_completed_barrier(monkeypatch, parties=2)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await FactoryService(session, storage=storage).create_build(
                    actor_user_id=user_id,
                    job_id=job_id,
                    candidate_ids=[uuid.UUID(str(cand_id))],
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    outcomes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert not errors, f"unexpected AppError in build race: {errors}"
    assert len(outcomes) == 2
    assert sorted(o.replayed for o in outcomes) == [False, True]
    assert {o.result.id for o in outcomes} == {outcomes[0].result.id}

    async with integration_session_factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM tool_factory_builds WHERE job_id = :jid"),
                {"jid": str(job_id)},
            )
        ).scalar_one()
        assert int(count) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_package_generate_same_idempotency_key(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same Idempotency-Key concurrent generate keeps a single package.

    Generation takes a Build row lock (FOR UPDATE), so a create_completed
    barrier would deadlock; gather without an artificial barrier instead.
    """
    storage = InMemoryObjectStorage()
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)
        outcome = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="pkg.json",
            idempotency_key=f"job-{uuid.uuid4().hex}",
        )
        await session.commit()
        job_id = outcome.result.id

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-pkg")
        result = await worker.process_job(job_id=job_id)
        await session.commit()
        assert result.status == JobStatus.SUCCEEDED.value
        cand_id = (
            await session.execute(
                text(
                    "SELECT id FROM tool_factory_candidates WHERE job_id = :jid LIMIT 1"
                ),
                {"jid": str(job_id)},
            )
        ).scalar_one()
        build = await FactoryService(session, storage=storage).create_build(
            actor_user_id=user_id,
            job_id=job_id,
            candidate_ids=[uuid.UUID(str(cand_id))],
            idempotency_key=f"build-{uuid.uuid4().hex}",
        )
        build_id = build.result.id

    key = f"pkg-race-{uuid.uuid4().hex}"

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await FactoryService(session, storage=storage).generate_package(
                    actor_user_id=user_id,
                    build_id=build_id,
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    outcomes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert not errors, f"unexpected AppError in package race: {errors}"
    assert len(outcomes) == 2
    assert sorted(o.replayed for o in outcomes) == [False, True]
    assert {o.result.id for o in outcomes} == {outcomes[0].result.id}
    assert {
        o.result.package_content_sha256 for o in outcomes
    } == {outcomes[0].result.package_content_sha256}

    async with integration_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tool_factory_generated_packages "
                    "WHERE build_id = :bid"
                ),
                {"bid": str(build_id)},
            )
        ).scalar_one()
        assert int(count) == 1
        tools = (
            await session.execute(text("SELECT count(*) FROM mcp_tools"))
        ).scalar_one()
        servers = (
            await session.execute(text("SELECT count(*) FROM mcp_servers"))
        ).scalar_one()
        assert int(tools) == 0
        assert int(servers) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_test_run_same_idempotency_key(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same Idempotency-Key concurrent TestRun create keeps a single row."""
    from app.services.factory_test_worker import FactoryTestWorkerService

    storage = InMemoryObjectStorage()
    async with integration_session_factory() as session:
        user_id = await _seed_manage_user(session)
        outcome = await FactoryService(
            session, storage=storage
        ).create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="tr.json",
            idempotency_key=f"job-{uuid.uuid4().hex}",
        )
        await session.commit()
        job_id = outcome.result.id

    async with integration_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="pg-tr")
        result = await worker.process_job(job_id=job_id)
        await session.commit()
        assert result.status == JobStatus.SUCCEEDED.value
        cand_id = (
            await session.execute(
                text(
                    "SELECT id FROM tool_factory_candidates WHERE job_id = :jid LIMIT 1"
                ),
                {"jid": str(job_id)},
            )
        ).scalar_one()
        build = await FactoryService(session, storage=storage).create_build(
            actor_user_id=user_id,
            job_id=job_id,
            candidate_ids=[uuid.UUID(str(cand_id))],
            idempotency_key=f"build-{uuid.uuid4().hex}",
        )
        pkg = await FactoryService(session, storage=storage).generate_package(
            actor_user_id=user_id,
            build_id=build.result.id,
            idempotency_key=f"pkg-{uuid.uuid4().hex}",
        )
        package_id = pkg.result.id

    key = f"tr-race-{uuid.uuid4().hex}"

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await FactoryService(session, storage=storage).create_test_run(
                    actor_user_id=user_id,
                    package_id=package_id,
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    outcomes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert not errors, f"unexpected AppError in test-run race: {errors}"
    assert len(outcomes) == 2
    assert sorted(o.replayed for o in outcomes) == [False, True]
    assert {o.result.id for o in outcomes} == {outcomes[0].result.id}

    async with integration_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tool_factory_test_runs "
                    "WHERE generated_package_id = :pid"
                ),
                {"pid": str(package_id)},
            )
        ).scalar_one()
        assert int(count) == 1
        outbox = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'FACTORY_TEST_RUN_DISPATCH' "
                    "AND aggregate_id = :tid"
                ),
                {"tid": str(outcomes[0].result.id)},
            )
        ).scalar_one()
        assert int(outbox) == 1

        worker = FactoryTestWorkerService(
            session, storage=storage, worker_id="pg-tr-run"
        )
        done = await worker.process_test_run(test_run_id=outcomes[0].result.id)
        await session.commit()
        assert done.status == JobStatus.SUCCEEDED.value

        again = await worker.process_test_run(test_run_id=outcomes[0].result.id)
        await session.commit()
        assert again.claimed is False

        tools = (
            await session.execute(text("SELECT count(*) FROM mcp_tools"))
        ).scalar_one()
        servers = (
            await session.execute(text("SELECT count(*) FROM mcp_servers"))
        ).scalar_one()
        assert int(tools) == 0
        assert int(servers) == 0
