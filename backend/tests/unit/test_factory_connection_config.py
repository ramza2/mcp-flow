"""Factory deferred DRAFT connection configuration + Connection Test (Issue #97)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.errors import AppError
from app.domain.enums import MCPServerStatus, MCPToolStatus, UserStatus
from app.mcp.errors import MCPClientError
from app.models.factory import ToolFactoryPublish
from app.models.mcp import MCPServerCheck, MCPServerDiscovery, MCPTool
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.mcp_server import MCPServerCreate, MCPServerUpdate
from app.services.mcp_discovery import MCPDiscoveryService
from app.services.mcp_server import (
    MCPServerService,
    connection_config_fingerprint_for_server,
    connection_deferred_effective,
    get_verified_connection_fingerprint,
    is_connection_deferred,
    is_factory_imported,
)
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"fcc-{uuid.uuid4().hex[:10]}",
            display_name="Factory Conn",
            email=f"fcc-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"fcc-r-{uuid.uuid4().hex[:8]}", name="FCC")
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


async def _create_factory_deferred(session: AsyncSession) -> uuid.UUID:
    svc = MCPServerService(session)
    server = await svc.create_factory_deferred_draft_uncommitted(
        name="Factory Deferred Conn",
        description="deferred",
        transport_config={
            "connection_deferred": True,
            "factory_publish": True,
            "source": "TOOL_FACTORY_PUBLISH",
            "publish_id": str(uuid.uuid4()),
            "review_id": str(uuid.uuid4()),
            "note": "lineage must survive",
        },
        code=f"fac-conn-{uuid.uuid4().hex[:12]}",
    )
    await session.commit()
    return server.id


def _healthy_http() -> MagicMock:
    http = MagicMock()
    http.discover_capabilities = AsyncMock(return_value={})
    http.list_tools = AsyncMock(return_value=[])
    return http


async def _connection_test_and_clean_discovery(
    session: AsyncSession, server_id: uuid.UUID
) -> None:
    """Issue #99: Factory Activate also requires CLEAN Discovery evidence."""

    # Helpers that attach durable FactoryPublish membership (Issue #99).
    from tests.unit import test_factory_discovery_reconcile as fdr

    existing_publish = (
        await session.execute(
            select(ToolFactoryPublish).where(
                ToolFactoryPublish.mcp_server_id == server_id
            )
        )
    ).scalar_one_or_none()

    tools_repo = MCPToolRepository(session)
    tools, _ = await tools_repo.list_tools(
        mcp_server_id=server_id, page=1, page_size=10_000
    )
    if existing_publish is None:
        user_id = await fdr._seed_user(session)
        review = await fdr._approved_review_row(session, user_id=user_id)
        if not tools:
            schema = {"type": "object", "properties": {}}
            tool = await tools_repo.create_tool(
                mcp_server_id=server_id,
                remote_name="GET /ping",
                display_name="GET /ping",
                tags=[],
                status=str(MCPToolStatus.INACTIVE),
            )
            version = await tools_repo.create_version(
                mcp_tool_id=tool.id,
                version_no=1,
                content_hash=uuid.uuid4().hex,
                validation_status="WARNING",
                input_schema=schema,
                output_schema=None,
                annotations={
                    "factory_import": True,
                    "operation_key": "GET /ping",
                    "method": "GET",
                    "path": "/ping",
                },
                raw_descriptor={
                    "method": "GET",
                    "path": "/ping",
                    "operation_key": "GET /ping",
                },
                schema_dialect="factory-declarative-v1",
            )
            tool.current_version_id = version.id
            await session.commit()
            tools = [tool]
        await fdr._attach_factory_publish(
            session,
            user_id=user_id,
            server_id=server_id,
            tool_ids=[tool.id for tool in tools],
            review=review,
        )
        tools, _ = await tools_repo.list_tools(
            mcp_server_id=server_id, page=1, page_size=10_000
        )

    remotes = []
    for tool in tools:
        assert tool.current_version_id is not None
        version = await tools_repo.get_version(tool.current_version_id)
        assert version is not None
        remotes.append(
            fdr._desc(
                tool.remote_name,
                input_schema=version.input_schema
                if isinstance(version.input_schema, dict)
                else {"type": "object"},
                output_schema=version.output_schema
                if isinstance(version.output_schema, dict)
                else None,
            )
        )

    http = MagicMock()
    http.discover_capabilities = AsyncMock(return_value={})
    http.list_tools = AsyncMock(return_value=remotes)
    await MCPDiscoveryService(session, http).connection_test(server_id)
    # Issue #107: Activate evidence requires CLEAN Apply, not preview.
    result = await MCPDiscoveryService(session, http).discover(
        server_id, apply_changes=True
    )
    assert result.success is True


def _failing_http() -> MagicMock:
    http = MagicMock()
    http.discover_capabilities = AsyncMock(
        side_effect=MCPClientError(
            error_layer="NETWORK",
            error_code="MCP_CONNECTION_FAILED",
            message="connection refused",
        )
    )
    http.list_tools = AsyncMock(
        side_effect=AssertionError("list_tools must not run after discover failure")
    )
    return http


@pytest.mark.asyncio
async def test_deferred_without_endpoint_connection_test_blocked(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    with pytest.raises(AppError) as exc:
        await MCPDiscoveryService(db_session, MagicMock()).connection_test(server_id)
    assert exc.value.code == "RESOURCE_CONFLICT"
    checks = (
        await db_session.execute(select(func.count()).select_from(MCPServerCheck))
    ).scalar_one()
    assert int(checks) == 0


@pytest.mark.asyncio
async def test_factory_draft_patch_endpoint_clears_deferred(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    before = await svc.get(server_id)
    assert is_connection_deferred(before) is True
    assert connection_deferred_effective(before) is True
    assert is_factory_imported(before) is True
    lineage_publish = before.transport_config.get("publish_id")
    lineage_note = before.transport_config.get("note")

    updated = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/factory"),
        expected_lock_version=int(before.lock_version),
    )
    assert updated.endpoint_url == "https://mcp.test/factory"
    assert updated.status == MCPServerStatus.DRAFT.value
    assert updated.transport_config.get("connection_deferred") is False
    assert updated.transport_config.get("factory_publish") is True
    assert updated.transport_config.get("source") == "TOOL_FACTORY_PUBLISH"
    assert updated.transport_config.get("publish_id") == lineage_publish
    assert updated.transport_config.get("note") == lineage_note
    assert is_connection_deferred(updated) is False
    assert connection_deferred_effective(updated) is False
    assert is_factory_imported(updated) is True


@pytest.mark.asyncio
async def test_factory_draft_invalid_endpoint_rejected(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    before = await svc.get(server_id)
    with pytest.raises(AppError) as exc:
        await svc.update(
            server_id,
            MCPServerUpdate(endpoint_url="ftp://unsafe.example/mcp"),
            expected_lock_version=int(before.lock_version),
        )
    assert exc.value.status_code == 422
    after = await svc.get(server_id)
    assert after.endpoint_url is None
    assert after.transport_config.get("connection_deferred") is True


@pytest.mark.asyncio
async def test_factory_draft_stale_lock_version_conflict(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    with pytest.raises(AppError) as exc:
        await svc.update(
            server_id,
            MCPServerUpdate(endpoint_url="https://mcp.test/factory"),
            expected_lock_version=999,
        )
    assert exc.value.code == "RESOURCE_VERSION_CONFLICT"
    after = await svc.get(server_id)
    assert after.endpoint_url is None
    assert after.transport_config.get("connection_deferred") is True


@pytest.mark.asyncio
async def test_connection_test_success_check_only_no_discovery_or_tool_mutation(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    before = await svc.get(server_id)
    await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/factory"),
        expected_lock_version=int(before.lock_version),
    )
    tool = await MCPToolRepository(db_session).create_tool(
        mcp_server_id=server_id,
        remote_name="imported_tool",
        display_name="imported_tool",
        tags=[],
        status=str(MCPToolStatus.INACTIVE),
    )
    await db_session.commit()

    result = await MCPDiscoveryService(
        db_session, _healthy_http()
    ).connection_test(server_id)
    assert result.status.value == "SUCCEEDED" or str(result.status) == "SUCCEEDED"

    checks = list(
        (
            await db_session.execute(
                select(MCPServerCheck).where(MCPServerCheck.mcp_server_id == server_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(checks) == 1
    assert checks[0].status == "SUCCEEDED"

    discoveries = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    assert int(discoveries) == 0

    refreshed_tool = await db_session.get(MCPTool, tool.id)
    assert refreshed_tool is not None
    assert refreshed_tool.status == MCPToolStatus.INACTIVE.value

    server = await svc.get(server_id)
    assert server.status == MCPServerStatus.DRAFT.value


@pytest.mark.asyncio
async def test_connection_test_failure_keeps_draft_blocks_activate(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    before = await svc.get(server_id)
    await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/factory"),
        expected_lock_version=int(before.lock_version),
    )

    result = await MCPDiscoveryService(
        db_session, _failing_http()
    ).connection_test(server_id)
    assert str(result.status) in {"FAILED", "MCPCheckStatus.FAILED"} or (
        getattr(result.status, "value", None) == "FAILED"
    )

    server = await svc.get(server_id)
    assert server.status == MCPServerStatus.DRAFT.value

    with pytest.raises(AppError) as act:
        await svc.activate(server_id)
    assert act.value.code == "RESOURCE_CONFLICT"
    assert "Connection Test" in act.value.message

    server_after = await svc.get(server_id)
    assert server_after.status == MCPServerStatus.DRAFT.value


@pytest.mark.asyncio
async def test_connection_test_success_does_not_auto_activate(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    before = await svc.get(server_id)
    await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/factory"),
        expected_lock_version=int(before.lock_version),
    )
    await MCPDiscoveryService(db_session, _healthy_http()).connection_test(server_id)
    server = await svc.get(server_id)
    assert server.status == MCPServerStatus.DRAFT.value


@pytest.mark.asyncio
async def test_factory_activate_without_successful_check_rejected(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    before = await svc.get(server_id)
    await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/factory"),
        expected_lock_version=int(before.lock_version),
    )
    with pytest.raises(AppError) as act:
        await svc.activate(server_id)
    assert act.value.code == "RESOURCE_CONFLICT"
    assert "Connection Test" in act.value.message


@pytest.mark.asyncio
async def test_factory_activate_endpoint_a_success_allowed(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    before = await svc.get(server_id)
    updated = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/a"),
        expected_lock_version=int(before.lock_version),
    )
    await _connection_test_and_clean_discovery(db_session, server_id)
    after_test = await svc.get(server_id)
    assert after_test.transport_config.get(
        "connection_test_verified_fingerprint"
    ) == connection_config_fingerprint_for_server(updated)

    activated = await svc.activate(server_id)
    assert activated.status == MCPServerStatus.ACTIVE.value

    discoveries = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    assert int(discoveries) >= 1


@pytest.mark.asyncio
async def test_factory_activate_blocked_after_endpoint_change_without_retest(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    s = await svc.get(server_id)
    s = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/a"),
        expected_lock_version=int(s.lock_version),
    )
    await _connection_test_and_clean_discovery(db_session, server_id)
    s = await svc.get(server_id)
    await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/b"),
        expected_lock_version=int(s.lock_version),
    )

    with pytest.raises(AppError) as act:
        await svc.activate(server_id)
    assert act.value.code == "RESOURCE_CONFLICT"
    assert "current connection" in act.value.message.lower()

    checks = (
        await db_session.execute(select(func.count()).select_from(MCPServerCheck))
    ).scalar_one()
    assert int(checks) >= 1  # historical Check retained


@pytest.mark.asyncio
async def test_factory_activate_allowed_after_retest_on_endpoint_b(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    s = await svc.get(server_id)
    s = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/a"),
        expected_lock_version=int(s.lock_version),
    )
    await _connection_test_and_clean_discovery(db_session, server_id)
    s = await svc.get(server_id)
    s = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/b"),
        expected_lock_version=int(s.lock_version),
    )
    with pytest.raises(AppError):
        await svc.activate(server_id)
    await _connection_test_and_clean_discovery(db_session, server_id)
    activated = await svc.activate(server_id)
    assert activated.status == MCPServerStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_factory_activate_blocked_after_auth_change(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    s = await svc.get(server_id)
    s = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/a"),
        expected_lock_version=int(s.lock_version),
    )
    await _connection_test_and_clean_discovery(db_session, server_id)
    s = await svc.get(server_id)
    secret_id = uuid.uuid4()
    await svc.update(
        server_id,
        MCPServerUpdate(auth_type="BEARER", auth_secret_id=secret_id),
        expected_lock_version=int(s.lock_version),
    )
    with pytest.raises(AppError) as act:
        await svc.activate(server_id)
    assert act.value.code == "RESOURCE_CONFLICT"
    assert "current connection" in act.value.message.lower()


@pytest.mark.asyncio
async def test_factory_activate_name_description_change_keeps_valid_test(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    s = await svc.get(server_id)
    s = await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/a"),
        expected_lock_version=int(s.lock_version),
    )
    await _connection_test_and_clean_discovery(db_session, server_id)
    s = await svc.get(server_id)
    await svc.update(
        server_id,
        MCPServerUpdate(name="Renamed Factory Server", description="meta only"),
        expected_lock_version=int(s.lock_version),
    )
    activated = await svc.activate(server_id)
    assert activated.status == MCPServerStatus.ACTIVE.value
    assert activated.name == "Renamed Factory Server"


@pytest.mark.asyncio
async def test_factory_activate_blocked_when_latest_check_failed(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    server_id = await _create_factory_deferred(db_session)
    svc = MCPServerService(db_session)
    s = await svc.get(server_id)
    await svc.update(
        server_id,
        MCPServerUpdate(endpoint_url="https://mcp.test/a"),
        expected_lock_version=int(s.lock_version),
    )
    await _connection_test_and_clean_discovery(db_session, server_id)
    await MCPDiscoveryService(db_session, _failing_http()).connection_test(server_id)

    with pytest.raises(AppError) as act:
        await svc.activate(server_id)
    assert act.value.code == "RESOURCE_CONFLICT"
    # Connection fingerprint still matches; latest FAILED Check blocks Activate.
    server = await svc.get(server_id)
    assert get_verified_connection_fingerprint(server) == (
        connection_config_fingerprint_for_server(server)
    )


@pytest.mark.asyncio
async def test_non_factory_server_activate_without_check_still_allowed(
    db_session: AsyncSession,
) -> None:
    """Ordinary MCP Servers must not gain Factory Connection Test preconditions."""

    await _seed_user(db_session)
    svc = MCPServerService(db_session)
    server = await svc.create(
        MCPServerCreate(
            name="Ordinary MCP",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/ordinary",
        )
    )
    assert is_factory_imported(server) is False
    activated = await svc.activate(server.id)
    assert activated.status == MCPServerStatus.ACTIVE.value
