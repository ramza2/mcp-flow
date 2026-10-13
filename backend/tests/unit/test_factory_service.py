"""Unit tests for Tool Factory async OpenAPI analysis service."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import JobStatus, UserStatus
from app.factory import (
    ARTIFACT_TYPE_OPENAPI_ANALYSIS,
    JOB_TYPE_OPENAPI_ANALYZE,
    OPENAPI_ANALYZER_VERSION,
)
from app.factory.errors import (
    FACTORY_ANALYSIS_LIMIT_EXCEEDED,
    FACTORY_CANDIDATE_PERSIST_FAILED,
    FACTORY_SOURCE_OBJECT_CORRUPT,
    FACTORY_SOURCE_OBJECT_MISSING,
    FACTORY_SOURCE_PARSE_ERROR,
)
from app.factory.openapi_analyzer import MAX_PATH_LEN, MAX_SOURCE_BYTES
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.models.factory import (
    ToolFactoryArtifact,
    ToolFactoryCandidate,
    ToolFactoryJob,
)
from app.models.idempotency import ApiIdempotencyRecord
from app.models.mcp import MCPServer, MCPTool
from app.models.outbox import OutboxEvent
from app.repositories.factory import FactoryRepository
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.services.factory import FactoryService, normalize_source_name
from app.services.factory_worker import FactoryWorkerService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.exc import DataError
from sqlalchemy.ext.asyncio import AsyncSession

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

MINIMAL_YAML = b"""\
openapi: 3.1.0
info:
  title: YAML Demo
  version: "1"
paths:
  /health:
    get:
      responses:
        200:
          description: ok
"""


async def _seed_user_with_perms(
    session: AsyncSession,
    *,
    codes: list[str],
    status: UserStatus = UserStatus.ACTIVE,
) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fac-{uuid.uuid4().hex[:10]}",
            display_name="Factory Tester",
            email=f"fac-{uuid.uuid4().hex[:8]}@example.com",
            status=status,
        )
    )
    if not codes:
        await session.commit()
        return user.id
    role = await RoleService(session).create(
        RoleCreate(code=f"fac-r-{uuid.uuid4().hex[:8]}", name="Factory Role")
    )
    perm_ids: list[uuid.UUID] = []
    for code in codes:
        perm = await PermissionRepository(session).get_by_code(code)
        assert perm is not None
        perm_ids.append(perm.id)
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=perm_ids),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()
    return user.id


def _svc(
    session: AsyncSession,
    *,
    storage: InMemoryObjectStorage | None = None,
) -> tuple[FactoryService, InMemoryObjectStorage]:
    store = storage or InMemoryObjectStorage()
    return FactoryService(session, storage=store), store


async def _run_worker(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    storage: InMemoryObjectStorage,
) -> None:
    worker = FactoryWorkerService(session, storage=storage, worker_id="unit-worker")
    outcome = await worker.process_job(job_id=job_id)
    await session.commit()
    assert outcome.claimed or outcome.reason in {"TERMINAL", "LEASE_HELD"}


@pytest.mark.asyncio
async def test_create_returns_queued_without_inline_analysis(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    assert outcome.http_status == 201
    assert outcome.replayed is False
    job = outcome.result
    assert job.status == JobStatus.QUEUED
    assert job.job_type == JOB_TYPE_OPENAPI_ANALYZE
    assert job.analyzer_version == OPENAPI_ANALYZER_VERSION
    assert job.source_format is None
    assert job.source_sha256 == hashlib.sha256(MINIMAL_JSON).hexdigest()
    assert job.operation_count == 0
    assert job.progress_current == 0

    job_row = await db_session.get(ToolFactoryJob, job.id)
    assert job_row is not None
    assert job_row.source_object_key is not None
    assert job_row.source_bucket is not None
    assert job_row.source_size_bytes == len(MINIMAL_JSON)
    assert not hasattr(job_row, "source_body")
    assert store.has_object(
        bucket=job_row.source_bucket,
        object_key=job_row.source_object_key,
    )
    outbox = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "FACTORY_JOB_DISPATCH",
                OutboxEvent.aggregate_id == job.id,
            )
        )
    ).scalar_one()
    assert outbox.published_at is None
    assert outbox.dedupe_key == f"factory_job:{job.id}:openapi_analyze"
    assert outbox.payload == {"job_id": str(job.id)}
    arts = (
        await db_session.execute(
            select(func.count()).where(ToolFactoryArtifact.job_id == job.id)
        )
    ).scalar_one()
    assert int(arts) == 0


@pytest.mark.asyncio
async def test_worker_completes_openapi_30_analysis(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)

    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.SUCCEEDED.value
    assert job_row.source_format == "JSON"
    assert job_row.operation_count == 1
    assert job_row.server_count == 1
    artifact = (
        await db_session.execute(
            select(ToolFactoryArtifact).where(
                ToolFactoryArtifact.job_id == outcome.result.id
            )
        )
    ).scalar_one()
    assert artifact.artifact_type == ARTIFACT_TYPE_OPENAPI_ANALYSIS
    assert artifact.inline_payload is not None
    dumped = json.dumps(artifact.inline_payload)
    assert MINIMAL_JSON.decode() not in dumped
    assert artifact.content_sha256 == compute_canonical_json_hash(artifact.inline_payload)


@pytest.mark.asyncio
async def test_worker_yaml_analysis_source_format(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_YAML,
        filename="demo.yaml",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.SUCCEEDED.value
    assert job_row.source_format == "YAML"


@pytest.mark.asyncio
async def test_worker_malformed_source_durable_failed(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    bad = b"{not-json"
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=bad,
        filename="bad.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    assert outcome.result.status == JobStatus.QUEUED
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.FAILED.value
    assert job_row.error_code == FACTORY_SOURCE_PARSE_ERROR
    assert "{not-json" not in (job_row.error_message or "")
    count = (
        await db_session.execute(
            select(func.count()).where(ToolFactoryArtifact.job_id == outcome.result.id)
        )
    ).scalar_one()
    assert int(count) == 0


@pytest.mark.asyncio
async def test_worker_missing_object_fails_safely(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    await store.delete_object(
        bucket=job_row.source_bucket or "",
        object_key=job_row.source_object_key or "",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.FAILED.value
    assert job_row.error_code == FACTORY_SOURCE_OBJECT_MISSING


@pytest.mark.asyncio
async def test_worker_corrupt_object_fails_safely(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    await store.put_bytes(
        bucket=job_row.source_bucket or "",
        object_key=job_row.source_object_key or "",
        data=b'{"openapi":"3.0.0","info":{"title":"x","version":"1"},"paths":{}}',
        content_type="application/json",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.FAILED.value
    assert job_row.error_code == FACTORY_SOURCE_OBJECT_CORRUPT


@pytest.mark.asyncio
async def test_worker_restart_reconstructs_from_object(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    # Simulate crashed RUNNING lease (expired).
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    from datetime import UTC, datetime, timedelta

    job_row.status = JobStatus.RUNNING.value
    job_row.worker_id = "dead-worker"
    job_row.lease_token = uuid.uuid4()
    job_row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=30)
    await db_session.commit()

    await _run_worker(db_session, job_id=outcome.result.id, storage=store)
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.SUCCEEDED.value
    arts = (
        await db_session.execute(
            select(func.count()).where(ToolFactoryArtifact.job_id == outcome.result.id)
        )
    ).scalar_one()
    assert int(arts) == 1


@pytest.mark.asyncio
async def test_artifact_hash_deterministic(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    store = InMemoryObjectStorage()
    svc, store = _svc(db_session, storage=store)
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
    await _run_worker(db_session, job_id=a.result.id, storage=store)
    await _run_worker(db_session, job_id=b.result.id, storage=store)
    art_a = (
        await db_session.execute(
            select(ToolFactoryArtifact).where(ToolFactoryArtifact.job_id == a.result.id)
        )
    ).scalar_one()
    art_b = (
        await db_session.execute(
            select(ToolFactoryArtifact).where(ToolFactoryArtifact.job_id == b.result.id)
        )
    ).scalar_one()
    assert art_a.content_sha256 == art_b.content_sha256


@pytest.mark.asyncio
async def test_permission_separation(db_session: AsyncSession) -> None:
    reader = await _seed_user_with_perms(db_session, codes=["mcp.server.read"])
    manager = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    store = InMemoryObjectStorage()
    svc, store = _svc(db_session, storage=store)
    with pytest.raises(AppError) as exc:
        await svc.create_openapi_analyze_job(
            actor_user_id=reader,
            source_bytes=MINIMAL_JSON,
            filename="x.json",
            idempotency_key=f"k-{uuid.uuid4().hex}",
        )
    assert exc.value.status_code == 403

    created = await svc.create_openapi_analyze_job(
        actor_user_id=manager,
        source_bytes=MINIMAL_JSON,
        filename="x.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=created.result.id, storage=store)
    detail = await svc.get_job(reader, created.result.id)
    assert detail.status == JobStatus.SUCCEEDED
    assert detail.analysis is not None


@pytest.mark.asyncio
async def test_inactive_user_rejected(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session,
        codes=["mcp.server.manage"],
        status=UserStatus.INACTIVE,
    )
    svc, _store = _svc(db_session)
    with pytest.raises(AppError) as exc:
        await svc.create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=MINIMAL_JSON,
            filename="x.json",
            idempotency_key=f"k-{uuid.uuid4().hex}",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_idempotency_replay_same_source(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, store = _svc(db_session)
    key = f"idem-{uuid.uuid4().hex}"
    first = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=key,
    )
    objects_after_first = store.object_count()
    second = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=key,
    )
    assert second.replayed is True
    assert second.result.id == first.result.id
    assert store.object_count() == objects_after_first
    count = (
        await db_session.execute(select(func.count()).select_from(ToolFactoryJob))
    ).scalar_one()
    assert int(count) == 1


@pytest.mark.asyncio
async def test_idempotency_conflict_changed_source(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, store = _svc(db_session)
    key = f"idem-{uuid.uuid4().hex}"
    await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=key,
    )
    before = store.object_count()
    other = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "Other", "version": "2"},
            "paths": {},
        }
    ).encode()
    with pytest.raises(AppError) as exc:
        await svc.create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=other,
            filename="demo.json",
            idempotency_key=key,
        )
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert exc.value.status_code == 409
    # Fast-path conflict before upload when idempotency already exists.
    assert store.object_count() == before


@pytest.mark.asyncio
async def test_idempotency_replay_keeps_single_job(db_session: AsyncSession) -> None:
    """Sequential same-key replay keeps a single Job (not a concurrency race)."""

    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    key = f"race-{uuid.uuid4().hex}"
    svc, store = _svc(db_session)
    first = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="race.json",
        idempotency_key=key,
    )
    first_id = first.result.id
    replay = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="race.json",
        idempotency_key=key,
    )
    assert replay.replayed is True
    assert replay.result.id == first_id
    row = (
        await db_session.execute(select(ApiIdempotencyRecord).limit(1))
    ).scalar_one()
    assert row.resource_type == "TOOL_FACTORY_JOB"
    assert row.resource_id == first_id
    jobs = (
        await db_session.execute(select(func.count()).select_from(ToolFactoryJob))
    ).scalar_one()
    assert int(jobs) == 1
    assert store.object_count() == 1


@pytest.mark.asyncio
async def test_oversize_rejected_without_job(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    huge = b"{" + (b"a" * (MAX_SOURCE_BYTES + 1))
    svc, store = _svc(db_session)
    with pytest.raises(AppError) as exc:
        await svc.create_openapi_analyze_job(
            actor_user_id=user_id,
            source_bytes=huge,
            filename="huge.json",
            idempotency_key=f"k-{uuid.uuid4().hex}",
        )
    assert exc.value.status_code == 413
    assert exc.value.code == "FACTORY_SOURCE_TOO_LARGE"
    count = (
        await db_session.execute(select(func.count()).select_from(ToolFactoryJob))
    ).scalar_one()
    assert int(count) == 0
    idemp = (
        await db_session.execute(select(func.count()).select_from(ApiIdempotencyRecord))
    ).scalar_one()
    assert int(idemp) == 0
    assert store.object_count() == 0


def test_normalize_source_name_strips_paths_and_controls() -> None:
    assert normalize_source_name("../../etc/passwd.json") == "passwd.json"
    assert normalize_source_name("C:\\\\temp\\\\spec.yaml") == "spec.yaml"
    assert normalize_source_name("a\x00b.json") == "ab.json"
    assert normalize_source_name(None) == "openapi.json"
    assert normalize_source_name("   ") == "openapi.json"


@pytest.mark.asyncio
async def test_no_raw_source_in_idempotency_snapshot(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    svc, _store = _svc(db_session)
    await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    row = (await db_session.execute(select(ApiIdempotencyRecord))).scalar_one()
    body = json.dumps(row.response_body)
    assert MINIMAL_JSON.decode() not in body
    assert "source_object_key" not in body


MULTI_OP_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "summary": "List pets",
                    "tags": ["pets"],
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "operationId": "createPet",
                    "summary": "Create pet",
                    "tags": ["pets"],
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/pets/{petId}": {
                "get": {
                    "operationId": "getPet",
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    },
    separators=(",", ":"),
).encode("utf-8")


@pytest.mark.asyncio
async def test_worker_materializes_candidate_tools(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MULTI_OP_JSON,
        filename="pets.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)

    candidates = (
        await db_session.execute(
            select(ToolFactoryCandidate).where(
                ToolFactoryCandidate.job_id == outcome.result.id
            )
        )
    ).scalars().all()
    assert len(candidates) == 3
    keys = {c.operation_key for c in candidates}
    assert keys == {"GET /pets", "POST /pets", "GET /pets/{petId}"}
    for c in candidates:
        assert c.source_sha256 == hashlib.sha256(MULTI_OP_JSON).hexdigest()
        assert c.analyzer_version == OPENAPI_ANALYZER_VERSION
        assert c.analysis_artifact_id is not None
        assert MULTI_OP_JSON.decode() not in json.dumps(
            {
                "parameters": c.parameters,
                "request_schema": c.request_schema,
                "response_schema": c.response_schema,
            }
        )

    listed = await svc.list_candidates_for_job(user_id, outcome.result.id)
    assert listed.total == 3
    assert listed.job_id == outcome.result.id
    assert len(listed.items) == 3

    detail = await svc.get_candidate(user_id, listed.items[0].id)
    assert detail.operation_key == listed.items[0].operation_key
    assert detail.job_id == outcome.result.id

    job_detail = await svc.get_job(user_id, outcome.result.id)
    assert job_detail.candidate_count == 3


@pytest.mark.asyncio
async def test_candidate_materialize_replay_idempotent(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MULTI_OP_JSON,
        filename="pets.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)

    job = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job is not None
    artifact = (
        await db_session.execute(
            select(ToolFactoryArtifact).where(
                ToolFactoryArtifact.job_id == job.id
            )
        )
    ).scalar_one()
    created_again = await svc.materialize_candidates_from_analysis(
        job=job,
        artifact=artifact,
    )
    assert created_again == 0
    count = (
        await db_session.execute(
            select(func.count()).where(
                ToolFactoryCandidate.job_id == outcome.result.id
            )
        )
    ).scalar_one()
    assert int(count) == 3

    # Explicit materialize endpoint is also idempotent.
    replay = await svc.materialize_candidates_for_job(user_id, outcome.result.id)
    assert replay.created_count == 0
    assert replay.total_count == 3


@pytest.mark.asyncio
async def test_candidate_list_is_readonly_materialize_is_durable(
    db_session: AsyncSession,
) -> None:
    """GET must not write; POST materialize commits durable candidates."""
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MULTI_OP_JSON,
        filename="pets.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)

    # Simulate a historical SUCCEEDED analysis without candidate rows.
    for row in (
        await db_session.execute(
            select(ToolFactoryCandidate).where(
                ToolFactoryCandidate.job_id == outcome.result.id
            )
        )
    ).scalars().all():
        await db_session.delete(row)
    await db_session.commit()

    empty = await svc.list_candidates_for_job(user_id, outcome.result.id)
    assert empty.total == 0
    # GET path must not have left uncommitted inserts.
    await db_session.commit()
    still_empty = (
        await db_session.execute(
            select(func.count()).where(
                ToolFactoryCandidate.job_id == outcome.result.id
            )
        )
    ).scalar_one()
    assert int(still_empty) == 0

    materialized = await svc.materialize_candidates_for_job(
        user_id, outcome.result.id
    )
    assert materialized.created_count == 3
    assert materialized.total_count == 3

    listed = await svc.list_candidates_for_job(user_id, outcome.result.id)
    assert listed.total == 3


@pytest.mark.asyncio
async def test_worker_candidate_materialize_all_or_nothing_on_mid_batch_failure(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial candidate inserts must not remain when Job fails persist."""
    user_id = await _seed_user_with_perms(
        db_session, codes=["mcp.server.manage", "mcp.server.read"]
    )
    calls = {"n": 0}
    original = FactoryRepository.create_candidate

    async def _flaky(self: FactoryRepository, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise DataError(
                "forced candidate persist failure",
                {},
                Exception("mock truncation after first insert"),
            )
        return await original(self, **kwargs)

    monkeypatch.setattr(FactoryRepository, "create_candidate", _flaky)

    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=MULTI_OP_JSON,
        filename="pets.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)

    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.FAILED.value
    assert job_row.error_code == FACTORY_CANDIDATE_PERSIST_FAILED
    assert calls["n"] >= 2

    cand_count = (
        await db_session.execute(
            select(func.count()).where(
                ToolFactoryCandidate.job_id == outcome.result.id
            )
        )
    ).scalar_one()
    assert int(cand_count) == 0

    tools = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    servers = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    assert int(tools) == 0
    assert int(servers) == 0


@pytest.mark.asyncio
async def test_worker_rejects_oversized_path_before_candidate_persist(
    db_session: AsyncSession,
) -> None:
    """Analyzer-invalid long path fails the Job; does not leave RUNNING."""
    user_id = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    long_path = "/" + ("z" * MAX_PATH_LEN)
    assert len(long_path) == MAX_PATH_LEN + 1
    source = json.dumps(
        {
            "openapi": "3.0.3",
            "info": {"title": "Long", "version": "1"},
            "paths": {
                long_path: {
                    "get": {"responses": {"200": {"description": "ok"}}},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=source,
        filename="long.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)
    job_row = await db_session.get(ToolFactoryJob, outcome.result.id)
    assert job_row is not None
    assert job_row.status == JobStatus.FAILED.value
    assert job_row.error_code == FACTORY_ANALYSIS_LIMIT_EXCEEDED
    cand_count = (
        await db_session.execute(
            select(func.count()).where(
                ToolFactoryCandidate.job_id == outcome.result.id
            )
        )
    ).scalar_one()
    assert int(cand_count) == 0


@pytest.mark.asyncio
async def test_candidate_read_requires_permission(db_session: AsyncSession) -> None:
    manager = await _seed_user_with_perms(db_session, codes=["mcp.server.manage"])
    stranger = await _seed_user_with_perms(db_session, codes=[])
    svc, store = _svc(db_session)
    outcome = await svc.create_openapi_analyze_job(
        actor_user_id=manager,
        source_bytes=MINIMAL_JSON,
        filename="demo.json",
        idempotency_key=f"k-{uuid.uuid4().hex}",
    )
    await _run_worker(db_session, job_id=outcome.result.id, storage=store)
    with pytest.raises(AppError) as exc:
        await svc.list_candidates_for_job(stranger, outcome.result.id)
    assert exc.value.status_code == 403
