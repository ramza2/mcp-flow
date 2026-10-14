"""Factory RuntimeBinding lifecycle + lineage + Discovery CLEAN (Issue #101)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from app.core.errors import AppError
from app.domain.enums import (
    FactoryRuntimeBindingStatus,
    JobStatus,
    MCPServerStatus,
    MCPToolStatus,
    UserStatus,
)
from app.factory.runtime_mcp import FactoryRuntimeMcpHandler
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.models.factory import ToolFactoryRuntimeBinding
from app.models.mcp import MCPTool
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.factory import FactoryRuntimeBindingUpdate
from app.schemas.mcp_server import MCPServerUpdate
from app.services.factory import FactoryService
from app.services.factory_runtime import FactoryRuntimeService
from app.services.factory_test_worker import FactoryTestWorkerService
from app.services.factory_worker import FactoryWorkerService
from app.services.mcp_discovery import MCPDiscoveryService
from app.services.mcp_server import (
    MCPServerService,
    get_verified_connection_fingerprint,
    get_verified_discovery_fingerprint,
)
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

PETS_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "parameters": [
                        {
                            "name": "status",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    },
    separators=(",", ":"),
).encode("utf-8")


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"frt-{uuid.uuid4().hex[:10]}",
            display_name="Factory Runtime",
            email=f"frt-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"frt-r-{uuid.uuid4().hex[:8]}", name="FRT")
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


async def _publish_pets(
    session: AsyncSession,
) -> tuple[FactoryService, InMemoryObjectStorage, uuid.UUID, uuid.UUID]:
    user_id = await _seed_user(session)
    store = InMemoryObjectStorage()
    svc = FactoryService(session, storage=store)
    job = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=PETS_JSON,
        filename="pets.json",
        idempotency_key=f"job-{uuid.uuid4().hex}",
    )
    await FactoryWorkerService(session, storage=store, worker_id="u-a").process_job(
        job_id=job.result.id
    )
    await session.commit()
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
    tr = await svc.create_test_run(
        actor_user_id=user_id,
        package_id=pkg.result.id,
        idempotency_key=f"tr-{uuid.uuid4().hex}",
    )
    done = await FactoryTestWorkerService(
        session, storage=store, worker_id="u-t"
    ).process_test_run(test_run_id=tr.result.id)
    await session.commit()
    assert done.status == JobStatus.SUCCEEDED.value
    review = await svc.create_review(
        actor_user_id=user_id,
        test_run_id=tr.result.id,
        decision="APPROVE",
        comment="ok",
        idempotency_key=f"rv-{uuid.uuid4().hex}",
    )
    published = await svc.publish_review(
        actor_user_id=user_id,
        review_id=review.result.id,
        idempotency_key=f"pub-{uuid.uuid4().hex}",
    )
    return svc, store, user_id, published.result.id


@pytest.mark.asyncio
async def test_runtime_binding_requires_valid_publish(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    with pytest.raises(AppError) as exc:
        await FactoryRuntimeService(db_session).create_binding(
            actor_user_id=user_id,
            publish_id=uuid.uuid4(),
        )
    assert exc.value.code == "FACTORY_RUNTIME_PUBLISH_INVALID"


@pytest.mark.asyncio
async def test_runtime_binding_happy_path_ready_endpoint_and_tools_list(
    db_session: AsyncSession,
) -> None:
    _svc, store, user_id, publish_id = await _publish_pets(db_session)
    runtime = FactoryRuntimeService(db_session, storage=store)
    created = await runtime.create_binding(
        actor_user_id=user_id, publish_id=publish_id
    )
    assert created.status == FactoryRuntimeBindingStatus.DRAFT
    assert created.mcp_endpoint_url is None

    updated = await runtime.update_binding(
        actor_user_id=user_id,
        binding_id=created.id,
        data=FactoryRuntimeBindingUpdate(
            base_url="http://factory-mock-upstream:8091",
            lock_version=created.lock_version,
        ),
    )
    assert updated.base_url == "http://factory-mock-upstream:8091"
    ready = await runtime.mark_ready(
        actor_user_id=user_id,
        binding_id=updated.id,
        lock_version=updated.lock_version,
    )
    assert ready.status == FactoryRuntimeBindingStatus.READY
    assert ready.mcp_endpoint_url is not None
    assert str(ready.id) in ready.mcp_endpoint_url

    row, contracts = await runtime.load_handler_context(ready.id)
    assert row.status == "READY"
    assert set(contracts) == {"GET /pets"}
    handler = FactoryRuntimeMcpHandler(
        binding_id=row.id,
        status_value=row.status,
        base_url=row.base_url,
        contracts_by_name=contracts,
        auth_headers=None,
        connect_timeout_ms=1000,
        call_timeout_ms=1000,
        max_response_bytes=10_000,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"pets": []})
            ),
            follow_redirects=False,
        ),
    )
    listed = await handler.handle(
        {"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}}
    )
    assert [t["name"] for t in listed["result"]["tools"]] == ["GET /pets"]


@pytest.mark.asyncio
async def test_lineage_mismatch_and_ssrf_blocked(db_session: AsyncSession) -> None:
    _svc, store, user_id, publish_id = await _publish_pets(db_session)
    runtime = FactoryRuntimeService(db_session, storage=store)
    created = await runtime.create_binding(
        actor_user_id=user_id, publish_id=publish_id
    )
    with pytest.raises(AppError) as ssrf:
        await runtime.update_binding(
            actor_user_id=user_id,
            binding_id=created.id,
            data=FactoryRuntimeBindingUpdate(
                base_url="http://127.0.0.1:8080",
                lock_version=created.lock_version,
            ),
        )
    assert ssrf.value.code in {
        "FACTORY_RUNTIME_BASE_URL_FORBIDDEN",
        "FACTORY_RUNTIME_CONFIG_INVALID",
    }

    # Tamper server transport_config review_id → READY lineage fail closed.
    pub = await _svc.get_publish(user_id, publish_id)
    server = await MCPServerService(db_session).get(pub.mcp_server_id)
    cfg = dict(server.transport_config or {})
    cfg["review_id"] = str(uuid.uuid4())
    server.transport_config = cfg
    await db_session.commit()
    refreshed = await runtime.get_binding(user_id, created.id)
    with pytest.raises(AppError) as lineage:
        await runtime.mark_ready(
            actor_user_id=user_id,
            binding_id=created.id,
            lock_version=refreshed.lock_version,
        )
    assert lineage.value.code == "FACTORY_RUNTIME_LINEAGE_MISMATCH"


@pytest.mark.asyncio
async def test_draft_endpoint_unavailable_ready_then_connection_discovery(
    db_session: AsyncSession,
) -> None:
    _svc, store, user_id, publish_id = await _publish_pets(db_session)
    runtime = FactoryRuntimeService(db_session, storage=store)
    created = await runtime.create_binding(
        actor_user_id=user_id, publish_id=publish_id
    )
    with pytest.raises(AppError):
        await runtime.load_handler_context(created.id)

    updated = await runtime.update_binding(
        actor_user_id=user_id,
        binding_id=created.id,
        data=FactoryRuntimeBindingUpdate(
            base_url="http://factory-mock-upstream:8091",
            lock_version=created.lock_version,
        ),
    )
    ready = await runtime.mark_ready(
        actor_user_id=user_id,
        binding_id=updated.id,
        lock_version=updated.lock_version,
    )
    pub = await _svc.get_publish(user_id, publish_id)
    server_svc = MCPServerService(db_session)
    server = await server_svc.get(pub.mcp_server_id)
    # Canonical endpoint provisioning path (Issue #97 fingerprint lifecycle).
    server = await server_svc.update(
        server.id,
        MCPServerUpdate(endpoint_url=ready.mcp_endpoint_url),
        expected_lock_version=int(server.lock_version),
    )
    assert server.endpoint_url == ready.mcp_endpoint_url

    # Build contracts once for mocked MCP HTTP.
    _row, contracts = await runtime.load_handler_context(ready.id)
    tool_schema = contracts["GET /pets"].input_schema

    async def _rpc(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        method = body.get("method")
        if method == "server/discover":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "not found"},
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "GET /pets",
                                "inputSchema": tool_schema,
                            }
                        ]
                    },
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_rpc))
    from app.mcp.client import MCPHttpClient

    mcp = MCPHttpClient(http=client)
    check = await MCPDiscoveryService(db_session, mcp).connection_test(server.id)
    assert check.status == "SUCCEEDED"
    result = await MCPDiscoveryService(db_session, mcp).discover(
        server.id, apply_changes=True
    )
    assert result.success is True
    assert result.factory_diff is not None
    assert result.factory_diff.status == "CLEAN"
    tools = (
        await db_session.execute(
            select(MCPTool).where(MCPTool.mcp_server_id == server.id)
        )
    ).scalars().all()
    assert {t.remote_name for t in tools} == {"GET /pets"}
    assert all(t.status == MCPToolStatus.INACTIVE.value for t in tools)
    assert get_verified_discovery_fingerprint(
        await server_svc.get(server.id)
    ) is not None
    # Unexpected tools not created.
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_duplicate_binding_fail_closed(db_session: AsyncSession) -> None:
    _svc, store, user_id, publish_id = await _publish_pets(db_session)
    runtime = FactoryRuntimeService(db_session, storage=store)
    await runtime.create_binding(actor_user_id=user_id, publish_id=publish_id)
    with pytest.raises(AppError) as exc:
        await runtime.create_binding(actor_user_id=user_id, publish_id=publish_id)
    assert exc.value.code == "FACTORY_RUNTIME_ALREADY_EXISTS"
    count = (
        await db_session.execute(
            select(func.count()).select_from(ToolFactoryRuntimeBinding)
        )
    ).scalar_one()
    assert int(count) == 1


@pytest.mark.asyncio
async def test_non_factory_discovery_unchanged(db_session: AsyncSession) -> None:
    await _seed_user(db_session)
    from app.schemas.mcp_server import MCPServerCreate

    server = await MCPServerService(db_session).create(
        MCPServerCreate(
            name="Ordinary",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/ordinary",
        )
    )
    http = MagicMock()
    http.discover_capabilities = AsyncMock(return_value={})
    from app.mcp.normalize import RemoteToolDescriptor

    http.list_tools = AsyncMock(
        return_value=[
            RemoteToolDescriptor(
                name="echo",
                description=None,
                input_schema={"type": "object"},
                output_schema=None,
                annotations=None,
                raw={"name": "echo"},
            )
        ]
    )
    result = await MCPDiscoveryService(db_session, http).discover(
        server.id, apply_changes=True
    )
    assert result.success is True
    assert result.factory_diff is None
    assert result.diff.added == 1


async def _ready_binding_with_active_factory_server(
    db_session: AsyncSession,
) -> tuple[FactoryRuntimeService, uuid.UUID, uuid.UUID, object]:
    """Publish → RuntimeBinding READY → Connection/Discovery → ACTIVE."""

    svc, store, user_id, publish_id = await _publish_pets(db_session)
    runtime = FactoryRuntimeService(db_session, storage=store)
    created = await runtime.create_binding(
        actor_user_id=user_id, publish_id=publish_id
    )
    updated = await runtime.update_binding(
        actor_user_id=user_id,
        binding_id=created.id,
        data=FactoryRuntimeBindingUpdate(
            base_url="http://factory-mock-upstream:8091",
            lock_version=created.lock_version,
        ),
    )
    ready = await runtime.mark_ready(
        actor_user_id=user_id,
        binding_id=updated.id,
        lock_version=updated.lock_version,
    )
    pub = await svc.get_publish(user_id, publish_id)
    server_svc = MCPServerService(db_session)
    server = await server_svc.get(pub.mcp_server_id)
    server = await server_svc.update(
        server.id,
        MCPServerUpdate(endpoint_url=ready.mcp_endpoint_url),
        expected_lock_version=int(server.lock_version),
    )
    _row, contracts = await runtime.load_handler_context(ready.id)
    tool_schema = contracts["GET /pets"].input_schema

    async def _rpc(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        method = body.get("method")
        if method == "server/discover":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "not found"},
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {"name": "GET /pets", "inputSchema": tool_schema}
                        ]
                    },
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    from app.mcp.client import MCPHttpClient

    mcp = MCPHttpClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(_rpc))
    )
    check = await MCPDiscoveryService(db_session, mcp).connection_test(server.id)
    assert check.status == "SUCCEEDED"
    result = await MCPDiscoveryService(db_session, mcp).discover(
        server.id, apply_changes=True
    )
    assert result.success is True
    activated = await server_svc.activate(server.id)
    assert activated.status == MCPServerStatus.ACTIVE.value
    assert get_verified_connection_fingerprint(activated) is not None
    assert get_verified_discovery_fingerprint(activated) is not None
    return runtime, user_id, ready.id, mcp


@pytest.mark.asyncio
async def test_runtime_binding_base_url_change_invalidates_factory_lifecycle(
    db_session: AsyncSession,
) -> None:
    runtime, user_id, binding_id, mcp = await _ready_binding_with_active_factory_server(
        db_session
    )
    before = await runtime.get_binding(user_id, binding_id)
    assert before.status == FactoryRuntimeBindingStatus.READY

    changed = await runtime.update_binding(
        actor_user_id=user_id,
        binding_id=binding_id,
        data=FactoryRuntimeBindingUpdate(
            base_url="http://factory-mock-upstream:8091/v2",
            lock_version=before.lock_version,
        ),
    )
    assert changed.status == FactoryRuntimeBindingStatus.DRAFT
    assert changed.mcp_endpoint_url is None

    server = await MCPServerService(db_session).get(changed.mcp_server_id)
    assert server.status == MCPServerStatus.INACTIVE.value
    assert get_verified_connection_fingerprint(server) is None
    assert get_verified_discovery_fingerprint(server) is None

    # re-READY alone must not allow Activate without fresh evidence.
    ready_again = await runtime.mark_ready(
        actor_user_id=user_id,
        binding_id=binding_id,
        lock_version=changed.lock_version,
    )
    assert ready_again.status == FactoryRuntimeBindingStatus.READY
    with pytest.raises(AppError) as act:
        await MCPServerService(db_session).activate(changed.mcp_server_id)
    assert act.value.code == "RESOURCE_CONFLICT"

    # Fresh Connection Test + Discovery restores Activate path.
    server = await MCPServerService(db_session).get(changed.mcp_server_id)
    server = await MCPServerService(db_session).update(
        server.id,
        MCPServerUpdate(endpoint_url=ready_again.mcp_endpoint_url),
        expected_lock_version=int(server.lock_version),
    )
    check = await MCPDiscoveryService(db_session, mcp).connection_test(server.id)
    assert check.status == "SUCCEEDED"
    disc = await MCPDiscoveryService(db_session, mcp).discover(
        server.id, apply_changes=True
    )
    assert disc.success is True
    activated = await MCPServerService(db_session).activate(server.id)
    assert activated.status == MCPServerStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_runtime_binding_disable_invalidates_factory_lifecycle(
    db_session: AsyncSession,
) -> None:
    runtime, user_id, binding_id, _mcp = await _ready_binding_with_active_factory_server(
        db_session
    )
    before = await runtime.get_binding(user_id, binding_id)
    disabled = await runtime.disable_binding(
        actor_user_id=user_id,
        binding_id=binding_id,
        lock_version=before.lock_version,
    )
    assert disabled.status == FactoryRuntimeBindingStatus.DISABLED
    server = await MCPServerService(db_session).get(disabled.mcp_server_id)
    assert server.status == MCPServerStatus.INACTIVE.value
    assert get_verified_connection_fingerprint(server) is None
    assert get_verified_discovery_fingerprint(server) is None
