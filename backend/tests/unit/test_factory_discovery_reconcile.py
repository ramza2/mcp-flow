"""Factory Discovery validation + imported Tool reconciliation (Issue #99)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import JobStatus, MCPServerStatus, MCPToolStatus, UserStatus
from app.factory import (
    FACTORY_PUBLISH_EVIDENCE_KIND,
    FACTORY_PUBLISH_EVIDENCE_SCHEMA_VERSION,
    PHASE_PUBLISH,
)
from app.factory.discovery_reconcile import (
    build_factory_reconcile_plan,
    factory_contract_fingerprint,
    has_immutable_factory_lineage,
    index_remote_tools_strict,
)
from app.factory.mcp_output_schema import factory_mcp_output_schema
from app.infrastructure.object_storage import InMemoryObjectStorage
from app.mcp.normalize import RemoteToolDescriptor
from app.models.factory import ToolFactoryReview
from app.models.mcp import MCPServerDiscovery, MCPTool, MCPToolVersion
from app.repositories.factory import FactoryRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.mcp_server import MCPServerUpdate
from app.services.factory import FactoryService
from app.services.factory_test_worker import FactoryTestWorkerService
from app.services.factory_worker import FactoryWorkerService
from app.services.mcp_discovery import MCPDiscoveryService
from app.services.mcp_server import (
    MCPServerService,
    get_verified_discovery_fingerprint,
    is_factory_imported,
)
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_MINIMAL_OPENAPI = json.dumps(
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


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fdr-{uuid.uuid4().hex[:10]}",
            display_name="Factory Disc",
            email=f"fdr-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fdr-r-{uuid.uuid4().hex[:8]}", name="FDR")
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


async def _approved_review_row(
    session: AsyncSession, *, user_id: uuid.UUID
) -> ToolFactoryReview:
    """Create an APPROVE Review to supply durable Publish lineage FKs."""

    store = InMemoryObjectStorage()
    svc = FactoryService(session, storage=store)
    job = await svc.create_openapi_analyze_job(
        actor_user_id=user_id,
        source_bytes=_MINIMAL_OPENAPI,
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
    row = await session.get(ToolFactoryReview, review.result.id)
    assert row is not None
    return row


async def _attach_factory_publish(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    server_id: uuid.UUID,
    tool_ids: list[uuid.UUID],
    review: ToolFactoryReview,
) -> uuid.UUID:
    """Persist FactoryPublish + bind Server transport_config lineage keys."""

    publish_id = uuid.uuid4()
    tool_id_strs = [str(tid) for tid in tool_ids]
    now = datetime.now(UTC)
    evidence = {
        "schema_version": FACTORY_PUBLISH_EVIDENCE_SCHEMA_VERSION,
        "kind": FACTORY_PUBLISH_EVIDENCE_KIND,
        "phase": PHASE_PUBLISH,
        "publish_id": str(publish_id),
        "review_id": str(review.id),
        "test_run_id": str(review.test_run_id),
        "generated_package_id": str(review.generated_package_id),
        "build_id": str(review.build_id),
        "job_id": str(review.job_id),
        "analysis_artifact_id": str(review.analysis_artifact_id),
        "package_content_sha256": review.package_content_sha256,
        "generator_version": review.generator_version,
        "package_schema_version": review.package_schema_version,
        "test_evidence_hash": review.test_evidence_hash,
        "review_evidence_hash": review.review_evidence_hash,
        "mcp_server_id": str(server_id),
        "mcp_tool_ids": tool_id_strs,
        "tool_count": len(tool_ids),
        "requested_by": str(user_id),
        "created_at": now.isoformat(),
        "connection_deferred": True,
    }
    await FactoryRepository(session).create_publish(
        publish_id=publish_id,
        review_id=review.id,
        test_run_id=review.test_run_id,
        generated_package_id=review.generated_package_id,
        build_id=review.build_id,
        job_id=review.job_id,
        analysis_artifact_id=review.analysis_artifact_id,
        package_content_sha256=review.package_content_sha256,
        generator_version=review.generator_version,
        package_schema_version=review.package_schema_version,
        test_evidence_hash=review.test_evidence_hash,
        review_evidence_hash=review.review_evidence_hash,
        mcp_server_id=server_id,
        mcp_tool_ids=tool_id_strs,
        tool_count=len(tool_ids),
        publish_evidence=evidence,
        publish_evidence_hash=compute_canonical_json_hash(evidence),
        requested_by=user_id,
    )
    server = await MCPServerService(session).get(server_id)
    cfg = dict(server.transport_config or {})
    cfg.update(
        {
            "factory_publish": True,
            "source": "TOOL_FACTORY_PUBLISH",
            "publish_id": str(publish_id),
            "review_id": str(review.id),
            "generated_package_id": str(review.generated_package_id),
            "package_content_sha256": review.package_content_sha256,
        }
    )
    server.transport_config = cfg
    await session.commit()
    return publish_id


async def _factory_server_ready(
    session: AsyncSession,
    *,
    tools: list[dict] | None = None,
) -> uuid.UUID:
    """Create Factory deferred server + durable Publish, Connection Test success."""

    user_id = await _seed_user(session)
    review = await _approved_review_row(session, user_id=user_id)
    publish_id = uuid.uuid4()
    svc = MCPServerService(session)
    server = await svc.create_factory_deferred_draft_uncommitted(
        name="Factory Discovery",
        description="disc",
        transport_config={
            "connection_deferred": True,
            "factory_publish": True,
            "source": "TOOL_FACTORY_PUBLISH",
            "publish_id": str(publish_id),
            "review_id": str(review.id),
            "generated_package_id": str(review.generated_package_id),
            "package_content_sha256": review.package_content_sha256,
        },
        code=f"fac-disc-{uuid.uuid4().hex[:12]}",
    )
    await session.commit()
    s = await svc.update(
        server.id,
        MCPServerUpdate(endpoint_url="https://mcp.test/factory-disc"),
        expected_lock_version=int(server.lock_version),
    )
    http = MagicMock()
    http.discover_capabilities = AsyncMock(return_value={})
    http.list_tools = AsyncMock(return_value=[])
    await MCPDiscoveryService(session, http).connection_test(s.id)

    tool_ids: list[uuid.UUID] = []
    if tools:
        repo = MCPToolRepository(session)
        for item in tools:
            tool = await repo.create_tool(
                mcp_server_id=s.id,
                remote_name=item["remote_name"],
                display_name=item["remote_name"],
                tags=[],
                status=str(MCPToolStatus.INACTIVE),
            )
            version = await repo.create_version(
                mcp_tool_id=tool.id,
                version_no=1,
                content_hash=uuid.uuid4().hex,
                validation_status="WARNING",
                input_schema=item.get("input_schema"),
                output_schema=item.get("output_schema"),
                annotations={
                    "factory_import": True,
                    "operation_key": item["remote_name"],
                    "method": "GET",
                    "path": "/pets",
                },
                raw_descriptor={
                    "method": "GET",
                    "path": "/pets",
                    "operation_key": item["remote_name"],
                },
                schema_dialect="factory-declarative-v1",
            )
            tool.current_version_id = version.id
            tool_ids.append(tool.id)
        await session.commit()

    if not tool_ids:
        raise AssertionError(
            "_factory_server_ready requires at least one imported Tool for "
            "FactoryPublish.mcp_tool_ids."
        )
    await _attach_factory_publish(
        session,
        user_id=user_id,
        server_id=s.id,
        tool_ids=tool_ids,
        review=review,
    )
    # Re-bind verified Connection Test fingerprint after transport_config rewrite.
    await MCPDiscoveryService(session, http).connection_test(s.id)
    return s.id


def _http_with_tools(remote_tools: list[RemoteToolDescriptor]) -> MagicMock:
    http = MagicMock()
    http.discover_capabilities = AsyncMock(return_value={})
    http.list_tools = AsyncMock(return_value=remote_tools)
    return http


def _desc(
    name: str,
    *,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    annotations: dict | None = None,
) -> RemoteToolDescriptor:
    return RemoteToolDescriptor(
        name=name,
        description="remote",
        input_schema=input_schema
        or {"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema=output_schema,
        annotations=annotations,
        raw={"name": name},
    )


def test_index_remote_tools_strict_duplicate() -> None:
    by_name, err = index_remote_tools_strict(
        [_desc("echo"), _desc("echo", input_schema={"type": "object"})]
    )
    assert by_name == {}
    assert err is not None
    assert "Duplicate" in err


def test_has_immutable_factory_lineage() -> None:
    assert (
        has_immutable_factory_lineage(
            annotations={"factory_import": True, "operation_key": "GET /pets"},
            schema_dialect="factory-declarative-v1",
        )
        is True
    )
    assert (
        has_immutable_factory_lineage(
            annotations={"factory_import": True},
            raw_descriptor={"method": "GET", "path": "/pets"},
        )
        is True
    )
    assert (
        has_immutable_factory_lineage(
            annotations={"factory_import": True},
            raw_descriptor={"name": "unrelated"},
        )
        is False
    )
    assert has_immutable_factory_lineage(annotations={"factory_import": False}) is False
    assert has_immutable_factory_lineage(annotations=None) is False


def test_contract_fingerprint_ignores_annotations_and_description() -> None:
    a = factory_contract_fingerprint(
        remote_name="t",
        input_schema={"type": "object"},
        output_schema=None,
    )
    b = factory_contract_fingerprint(
        remote_name="t",
        input_schema={"type": "object"},
        output_schema=None,
    )
    assert a == b
    plan = build_factory_reconcile_plan(
        expected_by_name={
            "t": {"input_schema": {"type": "object"}, "output_schema": None}
        },
        remote_by_name={
            "t": _desc(
                "t",
                input_schema={"type": "object"},
                annotations={"factory_import": True, "noise": 1},
            )
        },
    )
    assert plan.is_clean
    assert plan.entries[0].classification == "MATCHED"


def test_factory_reconcile_requires_wire_envelope_with_reviewed_body_schema() -> None:
    body_schema = {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    }
    input_schema = {"type": "object", "properties": {}}
    expected = {
        "GET /pets": {
            "input_schema": input_schema,
            "output_schema": body_schema,
        }
    }
    wire_schema = factory_mcp_output_schema(body_schema)
    assert wire_schema is not None
    matched = build_factory_reconcile_plan(
        expected_by_name=expected,
        remote_by_name={
            "GET /pets": _desc(
                "GET /pets", input_schema=input_schema, output_schema=wire_schema
            )
        },
    )
    assert matched.is_clean

    # Old bare-body advertisement must not silently pass a Factory Runtime
    # Discovery contract comparison.
    bare = build_factory_reconcile_plan(
        expected_by_name=expected,
        remote_by_name={
            "GET /pets": _desc(
                "GET /pets", input_schema=input_schema, output_schema=body_schema
            )
        },
    )
    assert bare.entries[0].classification == "CONTRACT_CHANGED"

    changed_envelope = {**wire_schema, "required": ["ok"]}
    drift = build_factory_reconcile_plan(
        expected_by_name=expected,
        remote_by_name={
            "GET /pets": _desc(
                "GET /pets",
                input_schema=input_schema,
                output_schema=changed_envelope,
            )
        },
    )
    assert drift.entries[0].classification == "CONTRACT_CHANGED"


def test_build_plan_classifications() -> None:
    expected = {
        "keep": {"input_schema": {"type": "object"}, "output_schema": None},
        "gone": {"input_schema": {"type": "object"}, "output_schema": None},
        "drift": {
            "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            "output_schema": None,
        },
    }
    remote = {
        "keep": _desc("keep", input_schema={"type": "object"}),
        "extra": _desc("extra"),
        "drift": _desc(
            "drift",
            input_schema={"type": "object", "properties": {"b": {"type": "number"}}},
        ),
    }
    plan = build_factory_reconcile_plan(expected_by_name=expected, remote_by_name=remote)
    assert plan.status == "MISMATCHED"
    by = {e.remote_name: e.classification for e in plan.entries}
    assert by["keep"] == "MATCHED"
    assert by["gone"] == "MISSING_EXPECTED"
    assert by["extra"] == "UNEXPECTED_REMOTE"
    assert by["drift"] == "CONTRACT_CHANGED"


@pytest.mark.asyncio
async def test_preview_exact_match_no_mutation(db_session: AsyncSession) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()

    result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=False)
    assert result.success is True
    assert result.factory_diff is not None
    assert result.factory_diff.status == "CLEAN"
    assert result.factory_diff.matched == 1
    assert result.diff.unchanged == 1

    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before
    tool = (
        await db_session.execute(select(MCPTool).where(MCPTool.mcp_server_id == server_id))
    ).scalar_one()
    assert tool.status == MCPToolStatus.INACTIVE.value
    assert get_verified_discovery_fingerprint(
        await MCPServerService(db_session).get(server_id)
    ) is None


@pytest.mark.asyncio
async def test_preview_missing_unexpected_contract_no_mutation(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[
            {"remote_name": "GET /pets", "input_schema": schema},
            {
                "remote_name": "GET /gone",
                "input_schema": {"type": "object"},
            },
        ],
    )
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()
    remote = [
        _desc(
            "GET /pets",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "number"}},
            },
        ),
        _desc("POST /other"),
    ]
    result = await MCPDiscoveryService(
        db_session, _http_with_tools(remote)
    ).discover(server_id, apply_changes=False)
    assert result.success is False
    assert result.factory_diff is not None
    assert result.factory_diff.status == "MISMATCHED"
    assert result.factory_diff.missing_expected == 1
    assert result.factory_diff.unexpected_remote == 1
    assert result.factory_diff.contract_changed == 1
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before
    server = await MCPServerService(db_session).get(server_id)
    assert get_verified_discovery_fingerprint(server) is None


@pytest.mark.asyncio
async def test_preview_duplicate_remote_fail_closed(db_session: AsyncSession) -> None:
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "echo", "input_schema": {"type": "object"}}],
    )
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()
    result = await MCPDiscoveryService(
        db_session,
        _http_with_tools([_desc("echo"), _desc("echo", input_schema={"type": "object"})]),
    ).discover(server_id, apply_changes=False)
    assert result.success is False
    assert result.factory_diff is not None
    assert result.factory_diff.status == "AMBIGUOUS"
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before


@pytest.mark.asyncio
async def test_apply_exact_match_reconciles_keeps_inactive(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=True)
    assert result.success is True
    assert result.factory_diff is not None
    assert result.factory_diff.status == "CLEAN"
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    tool = (
        await db_session.execute(select(MCPTool).where(MCPTool.mcp_server_id == server_id))
    ).scalar_one()
    assert tool.status == MCPToolStatus.INACTIVE.value
    server = await MCPServerService(db_session).get(server_id)
    assert server.status == MCPServerStatus.DRAFT.value
    assert get_verified_discovery_fingerprint(server) is not None
    version = await MCPToolRepository(db_session).get_version(tool.current_version_id)
    assert version is not None
    assert isinstance(version.annotations, dict)
    assert version.annotations.get("factory_import") is True


@pytest.mark.asyncio
async def test_apply_mismatch_zero_partial_mutation(db_session: AsyncSession) -> None:
    schema = {"type": "object"}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()
    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    with pytest.raises(AppError) as exc:
        await MCPDiscoveryService(
            db_session,
            _http_with_tools([_desc("GET /pets", input_schema=schema), _desc("extra")]),
        ).discover(server_id, apply_changes=True)
    assert exc.value.code == "FACTORY_DISCOVERY_RECONCILE_FAILED"
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before
    # Evidence retained; unexpected tool not created.
    discoveries = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    assert int(discoveries) >= 1
    names = {
        t.remote_name
        for t in (
            await db_session.execute(select(MCPTool).where(MCPTool.mcp_server_id == server_id))
        )
        .scalars()
        .all()
    }
    assert names == {"GET /pets"}


@pytest.mark.asyncio
async def test_factory_discovery_apply_preserves_http_body_schema(
    db_session: AsyncSession,
) -> None:
    """CLEAN Apply may version the wire descriptor but never import its envelope."""
    input_schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    body_schema = {
        "type": "object",
        "properties": {"pets": {"type": "array"}},
        "required": ["pets"],
    }
    server_id = await _factory_server_ready(
        db_session,
        tools=[
            {
                "remote_name": "GET /pets",
                "input_schema": input_schema,
                "output_schema": body_schema,
            }
        ],
    )
    http = _http_with_tools(
        [
            _desc(
                "GET /pets",
                input_schema=input_schema,
                output_schema=factory_mcp_output_schema(body_schema),
            )
        ]
    )
    discovery = MCPDiscoveryService(db_session, http)
    applied = await discovery.discover(server_id, apply_changes=True)
    assert applied.success is True
    assert applied.factory_diff is not None
    assert applied.factory_diff.status == "CLEAN"

    tool = (
        await db_session.execute(
            select(MCPTool).where(MCPTool.mcp_server_id == server_id)
        )
    ).scalar_one()
    current_version = await MCPToolRepository(db_session).get_version(
        tool.current_version_id
    )
    assert current_version is not None
    assert current_version.output_schema == body_schema

    # A second Discovery must compare the stored HTTP schema to the wire
    # envelope again, without accumulating nested envelopes.
    repeated = await discovery.discover(server_id, apply_changes=False)
    assert repeated.success is True
    assert repeated.factory_diff is not None
    assert repeated.factory_diff.status == "CLEAN"


@pytest.mark.asyncio
async def test_factory_activate_requires_discovery_then_allows(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    svc = MCPServerService(db_session)
    with pytest.raises(AppError) as before:
        await svc.activate(server_id)
    assert before.value.code == "RESOURCE_CONFLICT"
    assert "Discovery" in before.value.message

    await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=True)
    activated = await svc.activate(server_id)
    assert activated.status == MCPServerStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_factory_activate_blocked_after_config_drift_until_retest_rediscovery(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    svc = MCPServerService(db_session)
    http = _http_with_tools([_desc("GET /pets", input_schema=schema)])
    await MCPDiscoveryService(db_session, http).discover(server_id, apply_changes=True)
    s = await svc.get(server_id)
    s = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/factory-disc-b"),
        expected_lock_version=int(s.lock_version),
    )
    with pytest.raises(AppError) as drift:
        await svc.activate(server_id)
    assert drift.value.code == "RESOURCE_CONFLICT"

    # Retest + rediscovery restores Activate eligibility.
    await MCPDiscoveryService(db_session, http).connection_test(server_id)
    await MCPDiscoveryService(db_session, http).discover(server_id, apply_changes=True)
    activated = await svc.activate(server_id)
    assert activated.status == MCPServerStatus.ACTIVE.value
    # Historical discoveries retained (failed/mismatched stub + successes).
    discoveries = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    assert int(discoveries) >= 2


@pytest.mark.asyncio
async def test_discovery_without_connection_test_blocked(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    svc = MCPServerService(db_session)
    server = await svc.create_factory_deferred_draft_uncommitted(
        name="No Test",
        description=None,
        transport_config={
            "connection_deferred": True,
            "factory_publish": True,
            "source": "TOOL_FACTORY_PUBLISH",
        },
        code=f"fac-nt-{uuid.uuid4().hex[:12]}",
    )
    await db_session.commit()
    await svc.update(
        server.id,
        MCPServerUpdate(endpoint_url="https://mcp.test/no-test"),
        expected_lock_version=int(server.lock_version),
    )
    with pytest.raises(AppError) as exc:
        await MCPDiscoveryService(db_session, _http_with_tools([])).discover(
            server.id, apply_changes=False
        )
    assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_non_factory_discovery_apply_unchanged(db_session: AsyncSession) -> None:
    await _seed_user(db_session)
    svc = MCPServerService(db_session)
    from app.schemas.mcp_server import MCPServerCreate

    server = await svc.create(
        MCPServerCreate(
            name="Ordinary Disc",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/ordinary-disc",
        )
    )
    assert is_factory_imported(server) is False
    result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("echo")])
    ).discover(server.id, apply_changes=True)
    assert result.success is True
    assert result.factory_diff is None
    assert result.diff.added == 1
    tool = (
        await db_session.execute(select(MCPTool).where(MCPTool.mcp_server_id == server.id))
    ).scalar_one()
    assert tool.remote_name == "echo"
    assert tool.status == "DISCOVERED"


@pytest.mark.asyncio
async def test_unrelated_local_tool_not_absorbed_into_expected(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    repo = MCPToolRepository(db_session)
    unrelated = await repo.create_tool(
        mcp_server_id=server_id,
        remote_name="local.unrelated",
        display_name="local.unrelated",
        tags=[],
        status=str(MCPToolStatus.INACTIVE),
    )
    version = await repo.create_version(
        mcp_tool_id=unrelated.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status="WARNING",
        input_schema={"type": "object"},
        output_schema=None,
        annotations={"note": "not factory"},
        raw_descriptor={"name": "local.unrelated"},
        schema_dialect="json-schema",
    )
    unrelated.current_version_id = version.id
    await db_session.commit()

    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()

    result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=False)
    assert result.success is False
    assert result.factory_diff is not None
    assert result.factory_diff.status == "AMBIGUOUS"
    assert result.error_code == "FACTORY_DISCOVERY_AMBIGUOUS"
    assert "mcp_tool_ids" in (result.error_message or "")
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before
    names = {
        t.remote_name
        for t in (
            await db_session.execute(
                select(MCPTool).where(MCPTool.mcp_server_id == server_id)
            )
        )
        .scalars()
        .all()
    }
    assert names == {"GET /pets", "local.unrelated"}
    assert get_verified_discovery_fingerprint(
        await MCPServerService(db_session).get(server_id)
    ) is None

    apply_result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=True)
    assert apply_result.success is False
    assert apply_result.factory_diff is not None
    assert apply_result.factory_diff.status == "AMBIGUOUS"
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before


@pytest.mark.asyncio
async def test_factory_looking_tool_outside_publish_ids_fail_closed(
    db_session: AsyncSession,
) -> None:
    """Perfect Factory-looking metadata is insufficient without Publish membership."""

    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    repo = MCPToolRepository(db_session)
    fake = await repo.create_tool(
        mcp_server_id=server_id,
        remote_name="GET /forged",
        display_name="GET /forged",
        tags=[],
        status=str(MCPToolStatus.INACTIVE),
    )
    version = await repo.create_version(
        mcp_tool_id=fake.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status="WARNING",
        input_schema=schema,
        output_schema=None,
        annotations={
            "factory_import": True,
            "operation_key": "GET /forged",
            "method": "GET",
            "path": "/forged",
            "candidate_id": str(uuid.uuid4()),
        },
        raw_descriptor={
            "method": "GET",
            "path": "/forged",
            "operation_key": "GET /forged",
            "candidate_id": str(uuid.uuid4()),
        },
        schema_dialect="factory-declarative-v1",
    )
    fake.current_version_id = version.id
    await db_session.commit()

    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()
    result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=False)
    assert result.success is False
    assert result.factory_diff is not None
    assert result.factory_diff.status == "AMBIGUOUS"
    assert "mcp_tool_ids" in (result.error_message or "")
    assert "GET /forged" in (result.error_message or "")
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before


@pytest.mark.asyncio
async def test_transport_config_publish_lineage_mismatch_fail_closed(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    server = await MCPServerService(db_session).get(server_id)
    cfg = dict(server.transport_config or {})
    cfg["review_id"] = str(uuid.uuid4())
    server.transport_config = cfg
    await db_session.commit()

    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()
    result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=False)
    assert result.success is False
    assert result.factory_diff is not None
    assert result.factory_diff.status == "AMBIGUOUS"
    assert "review_id" in (result.error_message or "")
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before
    assert get_verified_discovery_fingerprint(
        await MCPServerService(db_session).get(server_id)
    ) is None


@pytest.mark.asyncio
async def test_normal_factory_publish_expected_set_loads(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    result = await MCPDiscoveryService(
        db_session, _http_with_tools([_desc("GET /pets", input_schema=schema)])
    ).discover(server_id, apply_changes=False)
    assert result.success is True
    assert result.factory_diff is not None
    assert result.factory_diff.status == "CLEAN"
    assert result.factory_diff.matched == 1
    # Issue #107: CLEAN preview must not store Activate discovery evidence.
    assert get_verified_discovery_fingerprint(
        await MCPServerService(db_session).get(server_id)
    ) is None


@pytest.mark.asyncio
async def test_clean_preview_blocks_activate_apply_enables(
    db_session: AsyncSession,
) -> None:
    """Issue #107: CLEAN preview ≠ Activate evidence; CLEAN Apply enables it."""

    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    http = _http_with_tools([_desc("GET /pets", input_schema=schema)])
    svc = MCPServerService(db_session)

    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()

    preview = await MCPDiscoveryService(db_session, http).discover(
        server_id, apply_changes=False
    )
    assert preview.success is True
    assert preview.factory_diff is not None
    assert preview.factory_diff.status == "CLEAN"
    server = await svc.get(server_id)
    assert get_verified_discovery_fingerprint(server) is None
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before
    with pytest.raises(AppError) as blocked:
        await svc.activate(server_id)
    assert blocked.value.code == "RESOURCE_CONFLICT"
    assert "Discovery" in blocked.value.message

    applied = await MCPDiscoveryService(db_session, http).discover(
        server_id, apply_changes=True
    )
    assert applied.success is True
    assert applied.factory_diff is not None
    assert applied.factory_diff.status == "CLEAN"
    server = await svc.get(server_id)
    assert get_verified_discovery_fingerprint(server) is not None
    activated = await svc.activate(server_id)
    assert activated.status == MCPServerStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_clean_preview_preserves_prior_apply_fingerprint(
    db_session: AsyncSession,
) -> None:
    """CLEAN preview must not clear a prior valid Apply discovery fingerprint."""

    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    http = _http_with_tools([_desc("GET /pets", input_schema=schema)])
    await MCPDiscoveryService(db_session, http).discover(
        server_id, apply_changes=True
    )
    fp_after_apply = get_verified_discovery_fingerprint(
        await MCPServerService(db_session).get(server_id)
    )
    assert fp_after_apply is not None

    preview = await MCPDiscoveryService(db_session, http).discover(
        server_id, apply_changes=False
    )
    assert preview.success is True
    assert preview.factory_diff is not None
    assert preview.factory_diff.status == "CLEAN"
    assert (
        get_verified_discovery_fingerprint(
            await MCPServerService(db_session).get(server_id)
        )
        == fp_after_apply
    )


@pytest.mark.asyncio
async def test_apply_connection_drift_zero_mutation(
    db_session: AsyncSession,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    server_id = await _factory_server_ready(
        db_session,
        tools=[{"remote_name": "GET /pets", "input_schema": schema}],
    )
    svc = MCPServerService(db_session)
    tools_before = (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one()
    versions_before = (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one()

    async def _list_tools_drift(*_a: object, **_k: object) -> list[RemoteToolDescriptor]:
        server = await svc.get(server_id)
        await svc.update(
            server_id,
            MCPServerUpdate(endpoint_url="https://mcp.test/factory-disc-drift-b"),
            expected_lock_version=int(server.lock_version),
        )
        return [_desc("GET /pets", input_schema=schema)]

    http = MagicMock()
    http.discover_capabilities = AsyncMock(return_value={})
    http.list_tools = AsyncMock(side_effect=_list_tools_drift)

    with pytest.raises(AppError) as exc:
        await MCPDiscoveryService(db_session, http).discover(
            server_id, apply_changes=True
        )
    assert exc.value.code == "FACTORY_DISCOVERY_CONNECTION_DRIFT"
    assert (
        await db_session.execute(select(func.count()).select_from(MCPTool))
    ).scalar_one() == tools_before
    assert (
        await db_session.execute(select(func.count()).select_from(MCPToolVersion))
    ).scalar_one() == versions_before
    server = await svc.get(server_id)
    assert get_verified_discovery_fingerprint(server) is None
    with pytest.raises(AppError) as act:
        await svc.activate(server_id)
    assert act.value.code == "RESOURCE_CONFLICT"
    discoveries = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    assert int(discoveries) >= 1
