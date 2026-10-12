"""SQLite-backed API tests for Tool Factory Jobs."""

from __future__ import annotations

import json
import uuid

import pytest
from app.domain.enums import JobStatus, UserStatus
from app.factory.openapi_analyzer import MAX_SOURCE_BYTES
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.services.role import RoleService
from app.services.user import UserService
from httpx import AsyncClient
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
    # Drop CSRF header after login.
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
    assert resp.json()["error"]["code"] == "AUTH_FORBIDDEN"


@pytest.mark.asyncio
async def test_read_allows_get_not_post(
    factory_client: AsyncClient,
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Same ASGI client cannot hold two sessions; create as manage, then re-login read-only.
    created = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    read_client = await _seed_client(
        unauthenticated_db_client,
        db_session_factory,
        codes=["mcp.server.read"],
    )
    listed = await read_client.get(f"{API}/jobs")
    assert listed.status_code == 200
    detail = await read_client.get(f"{API}/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["analysis"] is not None

    denied = await read_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_multipart_json_and_yaml(factory_client: AsyncClient) -> None:
    json_resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": f"k-json-{uuid.uuid4().hex}"},
    )
    assert json_resp.status_code == 201
    body = json_resp.json()
    assert body["status"] == JobStatus.SUCCEEDED.value
    assert body["source_format"] == "JSON"
    assert MINIMAL_JSON.decode() not in json_resp.text

    yaml_resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.yaml", MINIMAL_YAML, "application/yaml")},
        headers={"Idempotency-Key": f"k-yaml-{uuid.uuid4().hex}"},
    )
    assert yaml_resp.status_code == 201
    assert yaml_resp.json()["source_format"] == "YAML"


@pytest.mark.asyncio
async def test_empty_file_rejected(factory_client: AsyncClient) -> None:
    resp = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("empty.json", b"", "application/json")},
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 422
    assert "empty" not in resp.text.lower() or resp.json()["error"]["code"] == "VALIDATION_ERROR"


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
async def test_list_detail_pagination_filter_sort(factory_client: AsyncClient) -> None:
    for i in range(3):
        resp = await factory_client.post(
            f"{API}/jobs",
            files={"source": (f"demo-{i}.json", MINIMAL_JSON, "application/json")},
            headers={"Idempotency-Key": f"k-list-{i}-{uuid.uuid4().hex}"},
        )
        assert resp.status_code == 201

    bad = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("bad.json", b"{x", "application/json")},
        headers={"Idempotency-Key": f"k-bad-{uuid.uuid4().hex}"},
    )
    assert bad.status_code == 201
    assert bad.json()["status"] == JobStatus.FAILED.value

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
    assert detail.json()["analysis"] is None
    assert detail.json()["test_result_count"] == 0


@pytest.mark.asyncio
async def test_not_found(factory_client: AsyncClient) -> None:
    resp = await factory_client.get(f"{API}/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_idempotency_replay_and_conflict(factory_client: AsyncClient) -> None:
    key = f"idem-api-{uuid.uuid4().hex}"
    first = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201
    second = await factory_client.post(
        f"{API}/jobs",
        files={"source": ("demo.json", MINIMAL_JSON, "application/json")},
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]

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
