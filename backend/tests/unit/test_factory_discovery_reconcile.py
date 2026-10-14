"""Factory Discovery validation + imported Tool reconciliation (Issue #99)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.errors import AppError
from app.domain.enums import MCPServerStatus, MCPToolStatus, UserStatus
from app.factory.discovery_reconcile import (
    build_factory_reconcile_plan,
    factory_contract_fingerprint,
    index_remote_tools_strict,
)
from app.mcp.normalize import RemoteToolDescriptor
from app.models.mcp import MCPServerDiscovery, MCPTool, MCPToolVersion
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.mcp_server import MCPServerUpdate
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
    assert manage is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[manage.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()
    return user.id


async def _factory_server_ready(
    session: AsyncSession,
    *,
    tools: list[dict] | None = None,
) -> uuid.UUID:
    """Create Factory deferred server, set endpoint, Connection Test success."""

    await _seed_user(session)
    svc = MCPServerService(session)
    server = await svc.create_factory_deferred_draft_uncommitted(
        name="Factory Discovery",
        description="disc",
        transport_config={
            "connection_deferred": True,
            "factory_publish": True,
            "source": "TOOL_FACTORY_PUBLISH",
            "publish_id": str(uuid.uuid4()),
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
                raw_descriptor={"factory": True},
                schema_dialect="factory-declarative-v1",
            )
            tool.current_version_id = version.id
        await session.commit()
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
        _desc("GET /pets", input_schema={"type": "object", "properties": {"x": {"type": "number"}}}),
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
    assert get_verified_discovery_fingerprint(await MCPServerService(db_session).get(server_id)) is None


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
