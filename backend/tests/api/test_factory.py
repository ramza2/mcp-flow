"""SQLite-backed API tests for Tool Factory Jobs (async enqueue)."""

from __future__ import annotations

import json
import uuid

import pytest
from app.domain.enums import JobStatus, UserStatus
from app.factory.openapi_analyzer import MAX_SOURCE_BYTES
from app.infrastructure.object_storage import get_object_storage, reset_memory_object_storage
from app.models.factory import ToolFactoryJob
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.services.factory_worker import FactoryWorkerService
from app.services.role import RoleService
from app.services.user import UserService
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1/factory"

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

MINIMAL_YAML = b"""\
openapi: 3.1.0
info:
  title: YAML
  version: "1"
paths:
  /x:
    get:
      responses:
        200:
          description: ok
"""

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _fresh_memory_storage() -> None:
    reset_memory_object_storage()


async def _seed_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    codes: list[str],
) -> AsyncClient:
    from app.auth.passwords import hash_password

    client = unauthenticated_db_client
    username = f"fac-api-{uuid.uuid4().hex[:10]}"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="Factory API",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(user.id, hash_password(PASSWORD))
        user = await UserRepository(session).get(user.id)
        assert user is not None
        if codes:
            role = await RoleService(session).create(
                RoleCreate(code=f"fac-api-r-{uuid.uuid4().hex[:8]}", name="Fac API")
            )
            perm_ids = []
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

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client


async def _process_job(
    db_session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
) -> None:
    storage = get_object_storage()
    async with db_session_factory() as session:
        worker = FactoryWorkerService(session, storage=storage, worker_id="api-test")
        await worker.process_job(job_id=job_id)
        await session.commit()


@pytest.fixture
async def factory_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    return await _seed_client(
        unauthenticated_db_client,
        db_session_factory,
        codes=["mcp.server.read", "mcp.server.manage"],
    )


@pytest.fixture
async def factory_read_client(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    return await _seed_client(
        unauthenticated_db_client,
        db_session_factory,
        codes=["mcp.server.read"],
    )


@pytest.mark.asyncio
async def test_unauthenticated_rejected(unauthenticated_db_client: AsyncClient) -> None:
    resp = await unauthenticated_db_client.get(f"{API}/jobs")
    assert resp.status_code in {401, 403}


@pytest.mark.asyncio
async def test_csrf_required_on_post(factory_client: AsyncClient) -> None:
    factory_client.headers.pop("X-CSRF-Token", None)
    resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_manage_required_for_post(factory_read_client: AsyncClient) -> None:
    resp = await factory_read_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_read_allows_get_not_post(
    factory_client: AsyncClient,
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == JobStatus.QUEUED.value
    job_id = uuid.UUID(created.json()["id"])
    await _process_job(db_session_factory, job_id)

    read_client = await _seed_client(
        unauthenticated_db_client,
        db_session_factory,
        codes=["mcp.server.read"],
    )
    listed = await read_client.get(f"{API}/jobs")
    assert listed.status_code == 200
    detail = await read_client.get(f"{API}/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == JobStatus.SUCCEEDED.value
    assert detail.json()["analysis"] is not None

    denied = await read_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_multipart_json_and_yaml(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    json_resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-json-{uuid.uuid4().hex}"},
    )
    assert json_resp.status_code == 201
    body = json_resp.json()
    assert body["status"] == JobStatus.QUEUED.value
    assert body["source_format"] is None
    assert MINIMAL_JSON.decode() not in json_resp.text
    await _process_job(db_session_factory, uuid.UUID(body["id"]))
    detail = await factory_client.get(f"{API}/jobs/{body['id']}")
    assert detail.json()["status"] == JobStatus.SUCCEEDED.value
    assert detail.json()["source_format"] == "JSON"

    yaml_resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.yaml", MINIMAL_YAML, "application/yaml")},
        headers={"Idempotency-Key": f"k-yaml-{uuid.uuid4().hex}"},
    )
    assert yaml_resp.status_code == 201
    assert yaml_resp.json()["status"] == JobStatus.QUEUED.value
    await _process_job(db_session_factory, uuid.UUID(yaml_resp.json()["id"]))
    yaml_detail = await factory_client.get(f"{API}/jobs/{yaml_resp.json()['id']}")
    assert yaml_detail.json()["source_format"] == "YAML"


@pytest.mark.asyncio
async def test_empty_file_rejected(factory_client: AsyncClient) -> None:
    resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("empty.json", b"", "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_oversize_file_fail_closed(factory_client: AsyncClient) -> None:
    huge = b"{" + (b"a" * (MAX_SOURCE_BYTES + 1))
    resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("huge.json", huge, "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "FACTORY_SOURCE_TOO_LARGE"
    assert huge[:40].decode("latin-1") not in resp.text


@pytest.mark.asyncio
async def test_unsupported_extension(factory_client: AsyncClient) -> None:
    resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.txt", MINIMAL_JSON, "text/plain")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_detail_pagination_filter_sort(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for i in range(3):
        resp = await factory_client.post(
            f"{API}/jobs",
            files={"source": (f"demo-{i}.json", MINIMAL_JSON, "application/json")},
            headers={"Idempotency-Key": f"k-list-{i}-{uuid.uuid4().hex}"},
        )
        assert resp.status_code == 201
        await _process_job(db_session_factory, uuid.UUID(resp.json()["id"]))

    bad = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("bad.json", b"{x", "application/json")},
        headers={"Idempotency-Key": f"k-bad-{uuid.uuid4().hex}"},
    )
    assert bad.status_code == 201
    assert bad.json()["status"] == JobStatus.QUEUED.value
    await _process_job(db_session_factory, uuid.UUID(bad.json()["id"]))

    listed = await factory_client.get(
        f"{API}/jobs",
        params={"page": 1, "page_size": 2, "sort": "-created_at"},
    )
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert len(payload["items"]) == 2
    assert payload["has_next"] is True

    failed = await factory_client.get(
        f"{API}/jobs",
        params={"status": JobStatus.FAILED.value},
    )
    assert failed.status_code == 200
    assert all(item["status"] == JobStatus.FAILED.value for item in failed.json()["items"])

    q = await factory_client.get(f"{API}/jobs", params={"q": "demo-1"})
    assert q.status_code == 200
    assert any("demo-1" in item["source_name"] for item in q.json()["items"])

    job_id = bad.json()["id"]
    detail = await factory_client.get(f"{API}/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == JobStatus.FAILED.value
    assert detail.json()["analysis"] is None
    assert detail.json()["test_result_count"] == 0


@pytest.mark.asyncio
async def test_not_found(factory_client: AsyncClient) -> None:
    resp = await factory_client.get(f"{API}/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_idempotency_replay_and_conflict(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = f"idem-api-{uuid.uuid4().hex}"
    first = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201
    assert first.json()["status"] == JobStatus.QUEUED.value
    second = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

    async with db_session_factory() as session:
        jobs = list((await session.execute(select(ToolFactoryJob))).scalars().all())
        assert len(jobs) == 1

    other = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "X", "version": "1"},
            "paths": {},
        }
    ).encode()
    conflict = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", other, "application/json")},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


MULTI_OP_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "operationId": "createPet",
                    "responses": {"201": {"description": "created"}},
                },
            }
        },
    }
).encode("utf-8")


@pytest.mark.asyncio
async def test_candidate_list_and_detail(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("pets.json", MULTI_OP_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-cand-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    listed = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["job_id"] == job_id
    assert payload["total"] == 2
    assert len(payload["items"]) == 2
    keys = {item["operation_key"] for item in payload["items"]}
    assert keys == {"GET /pets", "POST /pets"}
    assert MULTI_OP_JSON.decode() not in listed.text
    assert "source_object_key" not in listed.text

    cand_id = payload["items"][0]["id"]
    detail = await factory_client.get(f"{API}/candidates/{cand_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == cand_id
    assert body["job_id"] == job_id
    assert body["analysis_artifact_id"]
    assert body["source_sha256"]
    assert body["analyzer_version"]
    assert body["operation_key"] in keys
    assert MULTI_OP_JSON.decode() not in detail.text

    job_detail = await factory_client.get(f"{API}/jobs/{job_id}")
    assert job_detail.json()["candidate_count"] == 2


@pytest.mark.asyncio
async def test_candidate_materialize_post_is_durable(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.factory import ToolFactoryCandidate
    from sqlalchemy import delete, func, select

    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("pets.json", MULTI_OP_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-mat-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    async with db_session_factory() as session:
        await session.execute(
            delete(ToolFactoryCandidate).where(
                ToolFactoryCandidate.job_id == uuid.UUID(job_id)
            )
        )
        await session.commit()

    empty = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    assert empty.status_code == 200
    assert empty.json()["total"] == 0

    async with db_session_factory() as session:
        count_after_get = (
            await session.execute(
                select(func.count()).where(
                    ToolFactoryCandidate.job_id == uuid.UUID(job_id)
                )
            )
        ).scalar_one()
        assert int(count_after_get) == 0

    materialized = await factory_client.post(
        f"{API}/jobs/{job_id}/candidates/materialize"
    )
    assert materialized.status_code == 200, materialized.text
    body = materialized.json()
    assert body["created_count"] == 2
    assert body["total_count"] == 2

    async with db_session_factory() as session:
        durable = (
            await session.execute(
                select(func.count()).where(
                    ToolFactoryCandidate.job_id == uuid.UUID(job_id)
                )
            )
        ).scalar_one()
        assert int(durable) == 2

    listed = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    assert listed.json()["total"] == 2


@pytest.mark.asyncio
async def test_candidate_list_requires_read(
    factory_client: AsyncClient,
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-cand-auth-{uuid.uuid4().hex}"},
    )
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    missing = await factory_client.get(f"{API}/candidates/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"

    no_perm = await _seed_client(
        unauthenticated_db_client,
        db_session_factory,
        codes=[],
    )
    denied = await no_perm.get(f"{API}/jobs/{job_id}/candidates")
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_build_create_list_detail(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("pets.json", MULTI_OP_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-build-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    cands = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    assert cands.status_code == 200
    cand_ids = [item["id"] for item in cands.json()["items"]]
    assert len(cand_ids) == 2

    build_resp = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": cand_ids[:1]},
        headers={"Idempotency-Key": f"build-{uuid.uuid4().hex}"},
    )
    assert build_resp.status_code == 201, build_resp.text
    body = build_resp.json()
    assert body["job_id"] == job_id
    assert body["candidate_count"] == 1
    assert body["candidates_snapshot"][0]["operation_key"]
    assert body["build_input_hash"]
    assert MULTI_OP_JSON.decode() not in build_resp.text
    assert "source_object_key" not in build_resp.text

    detail = await factory_client.get(f"{API}/builds/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == body["id"]
    assert detail.json()["candidates_snapshot_hash"] == body["candidates_snapshot_hash"]

    listed = await factory_client.get(f"{API}/jobs/{job_id}/builds")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == body["id"]

    empty = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": []},
        headers={"Idempotency-Key": f"build-{uuid.uuid4().hex}"},
    )
    assert empty.status_code == 422

    dup = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": [cand_ids[0], cand_ids[0]]},
        headers={"Idempotency-Key": f"build-{uuid.uuid4().hex}"},
    )
    assert dup.status_code == 422

    replay_key = f"build-replay-{uuid.uuid4().hex}"
    first = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": [cand_ids[1]]},
        headers={"Idempotency-Key": replay_key},
    )
    second = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": [cand_ids[1]]},
        headers={"Idempotency-Key": replay_key},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_generate_package_list_detail(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("pets.json", MULTI_OP_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-pkg-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    cands = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    cand_ids = [item["id"] for item in cands.json()["items"]]
    build_resp = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": cand_ids},
        headers={"Idempotency-Key": f"build-{uuid.uuid4().hex}"},
    )
    assert build_resp.status_code == 201
    build_id = build_resp.json()["id"]

    key = f"gen-{uuid.uuid4().hex}"
    first = await factory_client.post(
        f"{API}/builds/{build_id}/generate",
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["artifact_type"] == "GENERATED_PACKAGE"
    assert body["build_id"] == build_id
    assert body["tool_count"] == 2
    assert len(body["tools"]) == 2
    assert body["package_content_sha256"]
    assert MULTI_OP_JSON.decode() not in first.text
    assert "object_key" not in first.text
    assert "object_bucket" not in first.text
    assert "source_object_key" not in first.text

    replay = await factory_client.post(
        f"{API}/builds/{build_id}/generate",
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]
    assert replay.json()["package_content_sha256"] == body["package_content_sha256"]

    listed = await factory_client.get(f"{API}/builds/{build_id}/artifacts")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == body["id"]

    detail = await factory_client.get(f"{API}/artifacts/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["package_content_sha256"] == body["package_content_sha256"]
    assert len(detail.json()["tools"]) == 2


@pytest.mark.asyncio
async def test_sandbox_test_run_create_list_detail(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.factory_test_worker import FactoryTestWorkerService

    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("pets.json", MULTI_OP_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-tr-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    cands = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    cand_ids = [item["id"] for item in cands.json()["items"]]
    build_resp = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": cand_ids},
        headers={"Idempotency-Key": f"build-tr-{uuid.uuid4().hex}"},
    )
    assert build_resp.status_code == 201
    build_id = build_resp.json()["id"]

    pkg = await factory_client.post(
        f"{API}/builds/{build_id}/generate",
        headers={"Idempotency-Key": f"gen-tr-{uuid.uuid4().hex}"},
    )
    assert pkg.status_code == 201
    package_id = pkg.json()["id"]

    key = f"test-{uuid.uuid4().hex}"
    first = await factory_client.post(
        f"{API}/artifacts/{package_id}/tests",
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "QUEUED"
    assert body["generated_package_id"] == package_id
    assert body["security_policy_version"] == "factory-security-policy-v1"
    assert body["contract_policy_version"] == "factory-contract-policy-v1"
    assert body["result"] is None
    assert "object_key" not in first.text
    assert "object_bucket" not in first.text
    assert "evidence" not in body or body.get("evidence") is None

    replay = await factory_client.post(
        f"{API}/artifacts/{package_id}/tests",
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]

    storage = get_object_storage()
    async with db_session_factory() as session:
        worker = FactoryTestWorkerService(
            session, storage=storage, worker_id="api-test"
        )
        outcome = await worker.process_test_run(test_run_id=uuid.UUID(body["id"]))
        await session.commit()
        assert outcome.status == "SUCCEEDED"

    detail = await factory_client.get(f"{API}/test-runs/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "SUCCEEDED"
    assert detail.json()["result"] == "PASS"
    assert detail.json()["evidence_hash"]
    assert detail.json()["security_result"]["passed"] is True
    assert detail.json()["contract_result"]["passed"] is True
    assert "object_key" not in detail.text

    listed = await factory_client.get(f"{API}/artifacts/{package_id}/tests")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == body["id"]


@pytest.mark.asyncio
async def test_factory_review_approve_list_detail(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.factory_test_worker import FactoryTestWorkerService

    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("pets.json", MULTI_OP_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-rv-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    cands = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    cand_ids = [item["id"] for item in cands.json()["items"]]
    build_resp = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": cand_ids},
        headers={"Idempotency-Key": f"build-rv-{uuid.uuid4().hex}"},
    )
    assert build_resp.status_code == 201
    pkg = await factory_client.post(
        f"{API}/builds/{build_resp.json()['id']}/generate",
        headers={"Idempotency-Key": f"gen-rv-{uuid.uuid4().hex}"},
    )
    assert pkg.status_code == 201
    package_id = pkg.json()["id"]

    tr = await factory_client.post(
        f"{API}/artifacts/{package_id}/tests",
        headers={"Idempotency-Key": f"tr-rv-{uuid.uuid4().hex}"},
    )
    assert tr.status_code == 201
    test_run_id = tr.json()["id"]
    storage = get_object_storage()
    async with db_session_factory() as session:
        worker = FactoryTestWorkerService(
            session, storage=storage, worker_id="api-rv"
        )
        outcome = await worker.process_test_run(test_run_id=uuid.UUID(test_run_id))
        await session.commit()
        assert outcome.status == "SUCCEEDED"

    key = f"review-{uuid.uuid4().hex}"
    first = await factory_client.post(
        f"{API}/test-runs/{test_run_id}/review",
        json={"decision": "APPROVE", "comment": "ship it"},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["decision"] == "APPROVE"
    assert body["comment"] == "ship it"
    assert body["test_run_id"] == test_run_id
    assert body["review_evidence_hash"]
    assert "object_key" not in first.text
    assert "review_evidence" not in body or body.get("review_evidence") is None

    replay = await factory_client.post(
        f"{API}/test-runs/{test_run_id}/review",
        json={"decision": "APPROVE", "comment": "ship it"},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]

    detail = await factory_client.get(f"{API}/reviews/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == body["id"]

    by_run = await factory_client.get(f"{API}/test-runs/{test_run_id}/review")
    assert by_run.status_code == 200
    assert by_run.json()["id"] == body["id"]


@pytest.mark.asyncio
async def test_factory_publish_approve_detail(
    factory_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.factory_test_worker import FactoryTestWorkerService

    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("pets.json", MULTI_OP_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-pub-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    await _process_job(db_session_factory, uuid.UUID(job_id))

    cands = await factory_client.get(f"{API}/jobs/{job_id}/candidates")
    cand_ids = [item["id"] for item in cands.json()["items"]]
    build_resp = await factory_client.post(
        f"{API}/jobs/{job_id}/builds",
        json={"candidate_ids": cand_ids},
        headers={"Idempotency-Key": f"build-pub-{uuid.uuid4().hex}"},
    )
    assert build_resp.status_code == 201
    pkg = await factory_client.post(
        f"{API}/builds/{build_resp.json()['id']}/generate",
        headers={"Idempotency-Key": f"gen-pub-{uuid.uuid4().hex}"},
    )
    assert pkg.status_code == 201
    package_id = pkg.json()["id"]
    tool_count = pkg.json()["tool_count"]

    tr = await factory_client.post(
        f"{API}/artifacts/{package_id}/tests",
        headers={"Idempotency-Key": f"tr-pub-{uuid.uuid4().hex}"},
    )
    assert tr.status_code == 201
    test_run_id = tr.json()["id"]
    storage = get_object_storage()
    async with db_session_factory() as session:
        worker = FactoryTestWorkerService(
            session, storage=storage, worker_id="api-pub"
        )
        outcome = await worker.process_test_run(test_run_id=uuid.UUID(test_run_id))
        await session.commit()
        assert outcome.status == "SUCCEEDED"

    review = await factory_client.post(
        f"{API}/test-runs/{test_run_id}/review",
        json={"decision": "APPROVE", "comment": "ship"},
        headers={"Idempotency-Key": f"rv-pub-{uuid.uuid4().hex}"},
    )
    assert review.status_code == 201, review.text
    review_id = review.json()["id"]

    key = f"publish-{uuid.uuid4().hex}"
    first = await factory_client.post(
        f"{API}/reviews/{review_id}/publish",
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["review_id"] == review_id
    assert body["tool_count"] == tool_count
    assert len(body["mcp_tool_ids"]) == tool_count
    assert body["mcp_server_id"]
    assert body["publish_evidence_hash"]
    assert "object_key" not in first.text
    assert "publish_evidence" not in body or body.get("publish_evidence") is None

    replay = await factory_client.post(
        f"{API}/reviews/{review_id}/publish",
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]

    detail = await factory_client.get(f"{API}/publishes/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == body["id"]

    by_review = await factory_client.get(f"{API}/reviews/{review_id}/publish")
    assert by_review.status_code == 200
    assert by_review.json()["id"] == body["id"]
