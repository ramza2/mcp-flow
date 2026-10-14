"""Unit tests for Tool Factory Publish/Import foundation (Issue #95)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from app.agent.parameter_builder import _validate_input_schema
from app.core.errors import AppError
from app.domain.enums import JobStatus, MCPServerStatus, MCPToolStatus, UserStatus
from app.factory.errors import (
    FACTORY_PACKAGE_HASH_MISMATCH,
    FACTORY_PACKAGE_LINEAGE_MISMATCH,
    FACTORY_PUBLISH_ALREADY_EXISTS,
    FACTORY_PUBLISH_EVIDENCE_MISMATCH,
    FACTORY_PUBLISH_REJECTED_REVIEW,
)
from app.factory.package_generator import encode_canonical_json_bytes
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.models.factory import (
    ToolFactoryGeneratedPackage,
    ToolFactoryPublish,
    ToolFactoryReview,
    ToolFactoryTestRun,
)
from app.models.mcp import (
    MCPServer,
    MCPServerCheck,
    MCPServerDiscovery,
    MCPTool,
    MCPToolVersion,
)
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.services.factory import FactoryService
from app.services.factory_test_worker import FactoryTestWorkerService
from app.services.factory_worker import FactoryWorkerService
from app.services.mcp_discovery import MCPDiscoveryService
from app.services.mcp_server import MCPServerService
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
            }
        },
    },
    separators=(",", ":"),
).encode("utf-8")

RICH_JSON = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1.0.0"},
        "paths": {
            "/pets/{petId}": {
                "put": {
                    "operationId": "updatePet",
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "format": "int64"},
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                        {
                            "name": "q",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"],
                                }
                            }
                        },
                    },
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
            username=f"fac-pub-{uuid.uuid4().hex[:10]}",
            display_name="Factory Publish",
            email=f"fac-pub-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fac-pub-r-{uuid.uuid4().hex[:8]}", name="Fac Publish")
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


async def _approved_review(
    session: AsyncSession,
    *,
    decision: str = "APPROVE",
    source_bytes: bytes = MINIMAL_JSON,
) -> tuple[FactoryService, InMemoryObjectStorage, uuid.UUID, uuid.UUID]:
    user_id = await _seed_user(session)
    store = InMemoryObjectStorage()
    svc = FactoryService(session, storage=store)
    job = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=source_bytes,
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
        decision=decision,
        comment="ok" if decision == "APPROVE" else "no",
        idempotency_key=f"rv-{uuid.uuid4().hex}",
    )
    return svc, store, user_id, review.result.id


@pytest.mark.asyncio
async def test_publish_approve_happy_path(db_session: AsyncSession) -> None:
    svc, _store, user_id, review_id = await _approved_review(
        db_session, source_bytes=RICH_JSON
    )
    key = f"pub-{uuid.uuid4().hex}"
    published = await svc.publish_review(
        actor_user_id=user_id,
        review_id=review_id,
        idempotency_key=key,
    )
    assert published.http_status == 201
    assert published.replayed is False
    assert published.result.review_id == review_id
    assert published.result.tool_count >= 1
    assert len(published.result.mcp_tool_ids) == published.result.tool_count
    assert len(published.result.publish_evidence_hash) == 64

    server = await db_session.get(MCPServer, published.result.mcp_server_id)
    assert server is not None
    assert server.status == MCPServerStatus.DRAFT.value
    assert server.endpoint_url is None
    assert isinstance(server.transport_config, dict)
    assert server.transport_config.get("connection_deferred") is True

    with pytest.raises(AppError) as act:
        await MCPServerService(db_session).activate(server.id)
    assert act.value.code == "RESOURCE_CONFLICT"
    with pytest.raises(AppError) as conn:
        await MCPDiscoveryService(db_session, MagicMock()).connection_test(server.id)
    assert conn.value.code == "RESOURCE_CONFLICT"

    tools = list(
        (
            await db_session.execute(
                select(MCPTool).where(MCPTool.mcp_server_id == server.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(tools) == published.result.tool_count
    for tool in tools:
        assert tool.status == MCPToolStatus.INACTIVE.value
        assert tool.current_version_id is not None
        version = await db_session.get(MCPToolVersion, tool.current_version_id)
        assert version is not None
        assert isinstance(version.raw_descriptor, dict)
        assert version.raw_descriptor.get("operation_key")
        assert version.raw_descriptor.get("method")
        assert version.raw_descriptor.get("path")
        assert "command" not in version.raw_descriptor
        assert "secrets" not in version.raw_descriptor
        assert isinstance(version.input_schema, dict)
        props, required = _validate_input_schema(version.input_schema)
        assert "petId" in props
        assert "q" in props
        assert "verbose" in props
        assert "body" in props
        assert "petId" in required
        assert "q" in required
        assert "body" in required
        assert "verbose" not in required
        assert isinstance(version.raw_descriptor.get("parameters"), list)
        assert version.raw_descriptor.get("request_schema") is not None

    package = await db_session.get(
        ToolFactoryGeneratedPackage, published.result.generated_package_id
    )
    assert package is not None
    assert published.result.tool_count == package.tool_count

    replay = await svc.publish_review(
        actor_user_id=user_id,
        review_id=review_id,
        idempotency_key=key,
    )
    assert replay.replayed is True
    assert replay.result.id == published.result.id

    detail = await svc.get_publish(user_id, published.result.id)
    assert detail.id == published.result.id
    by_review = await svc.get_publish_for_review(user_id, review_id)
    assert by_review.id == published.result.id

    # No activation / discovery / connection-check side effects.
    servers = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    assert int(servers) == 1
    discoveries = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    checks = (
        await db_session.execute(select(func.count()).select_from(MCPServerCheck))
    ).scalar_one()
    assert int(discoveries) == 0
    assert int(checks) == 0


@pytest.mark.asyncio
async def test_publish_reject_blocked_zero_side_effects(
    db_session: AsyncSession,
) -> None:
    svc, _store, user_id, review_id = await _approved_review(
        db_session, decision="REJECT"
    )
    servers_before = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    with pytest.raises(AppError) as exc:
        await svc.publish_review(
            actor_user_id=user_id,
            review_id=review_id,
            idempotency_key=f"pub-{uuid.uuid4().hex}",
        )
    assert exc.value.code == FACTORY_PUBLISH_REJECTED_REVIEW
    servers_after = (
        await db_session.execute(select(func.count()).select_from(MCPServer))
    ).scalar_one()
    tools_after = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    publishes = (
        await db_session.execute(select(func.count()).select_from(ToolFactoryPublish))
    ).scalar_one()
    assert int(servers_before) == int(servers_after) == 0
    assert int(tools_before) == int(tools_after) == 0
    assert int(publishes) == 0


@pytest.mark.asyncio
async def test_publish_rejects_tamper_and_package_mismatch(
    db_session: AsyncSession,
) -> None:
    # Review evidence hash tamper.
    svc, _store, user_id, review_id = await _approved_review(db_session)
    review = await db_session.get(ToolFactoryReview, review_id)
    assert review is not None
    review.review_evidence_hash = "a" * 64
    await db_session.commit()
    with pytest.raises(AppError) as rev:
        await svc.publish_review(
            actor_user_id=user_id,
            review_id=review_id,
            idempotency_key=f"pub-{uuid.uuid4().hex}",
        )
    assert rev.value.code == FACTORY_PUBLISH_EVIDENCE_MISMATCH

    # TestRun evidence/lineage tamper.
    svc2, _s2, user_id2, review_id2 = await _approved_review(db_session)
    review2 = await db_session.get(ToolFactoryReview, review_id2)
    assert review2 is not None
    tr = await db_session.get(ToolFactoryTestRun, review2.test_run_id)
    assert tr is not None
    tr.package_content_sha256 = "b" * 64
    await db_session.commit()
    with pytest.raises(AppError) as lineage:
        await svc2.publish_review(
            actor_user_id=user_id2,
            review_id=review_id2,
            idempotency_key=f"pub-{uuid.uuid4().hex}",
        )
    assert lineage.value.code == FACTORY_PUBLISH_EVIDENCE_MISMATCH

    # Package missing.
    svc3, store3, user_id3, review_id3 = await _approved_review(db_session)
    review3 = await db_session.get(ToolFactoryReview, review_id3)
    assert review3 is not None
    package = await db_session.get(
        ToolFactoryGeneratedPackage, review3.generated_package_id
    )
    assert package is not None
    await store3.delete_object(
        bucket=package.object_bucket,
        object_key=package.object_key,
    )
    with pytest.raises(AppError) as missing:
        await svc3.publish_review(
            actor_user_id=user_id3,
            review_id=review_id3,
            idempotency_key=f"pub-{uuid.uuid4().hex}",
        )
    assert missing.value.status_code in {409, 503}

    # Package hash mismatch.
    svc4, store4, user_id4, review_id4 = await _approved_review(db_session)
    review4 = await db_session.get(ToolFactoryReview, review_id4)
    assert review4 is not None
    package4 = await db_session.get(
        ToolFactoryGeneratedPackage, review4.generated_package_id
    )
    assert package4 is not None
    original = await store4.get_bytes(
        bucket=package4.object_bucket,
        object_key=package4.object_key,
    )
    payload = json.loads(original.decode("utf-8"))
    payload["tools"][0]["summary"] = "tampered"
    await store4.put_bytes(
        bucket=package4.object_bucket,
        object_key=package4.object_key,
        data=encode_canonical_json_bytes(payload),
        content_type="application/json",
    )
    with pytest.raises(AppError) as hash_exc:
        await svc4.publish_review(
            actor_user_id=user_id4,
            review_id=review_id4,
            idempotency_key=f"pub-{uuid.uuid4().hex}",
        )
    assert hash_exc.value.code in {
        FACTORY_PACKAGE_HASH_MISMATCH,
        FACTORY_PACKAGE_LINEAGE_MISMATCH,
    }


@pytest.mark.asyncio
async def test_publish_idempotency_conflict_and_already_exists(
    db_session: AsyncSession,
) -> None:
    svc, _store, user_id, review_id = await _approved_review(db_session)
    key = f"pub-idem-{uuid.uuid4().hex}"
    first = await svc.publish_review(
        actor_user_id=user_id,
        review_id=review_id,
        idempotency_key=key,
    )
    # Same key, different review → conflict. Use a second APPROVE review.
    _svc2, _s2, _uid2, review_id2 = await _approved_review(db_session)
    with pytest.raises(AppError) as conflict:
        await svc.publish_review(
            actor_user_id=user_id,
            review_id=review_id2,
            idempotency_key=key,
        )
    assert conflict.value.code == "IDEMPOTENCY_KEY_REUSED"

    with pytest.raises(AppError) as exists:
        await svc.publish_review(
            actor_user_id=user_id,
            review_id=review_id,
            idempotency_key=f"pub-other-{uuid.uuid4().hex}",
        )
    assert exists.value.code == FACTORY_PUBLISH_ALREADY_EXISTS
    assert first.result.mcp_server_id

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(ToolFactoryPublish)
            .where(ToolFactoryPublish.review_id == review_id)
        )
    ).scalar_one()
    assert int(count) == 1


@pytest.mark.asyncio
async def test_publish_one_terminal_row_no_activation(
    db_session: AsyncSession,
) -> None:
    """SQLite StaticPool cannot host true write races; PG covers concurrent publish."""

    svc, _store, user_id, review_id = await _approved_review(db_session)
    first = await svc.publish_review(
        actor_user_id=user_id,
        review_id=review_id,
        idempotency_key=f"pub-term-{uuid.uuid4().hex}",
    )
    with pytest.raises(AppError) as second:
        await svc.publish_review(
            actor_user_id=user_id,
            review_id=review_id,
            idempotency_key=f"pub-term2-{uuid.uuid4().hex}",
        )
    assert second.value.code == FACTORY_PUBLISH_ALREADY_EXISTS
    server = await db_session.get(MCPServer, first.result.mcp_server_id)
    assert server is not None
    assert server.status == MCPServerStatus.DRAFT.value
    assert server.discovery_mode is None
    assert server.last_healthy_at is None
