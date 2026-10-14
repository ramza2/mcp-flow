"""Canonical deferred-DRAFT MCP Server create / activate / connection-test gates."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from app.core.errors import AppError
from app.domain.enums import MCPServerStatus, MCPTransportType, UserStatus
from app.models.mcp import MCPServerCheck, MCPServerDiscovery
from app.repositories.role import PermissionRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.schemas.mcp_server import MCPServerCreate
from app.services.mcp_discovery import MCPDiscoveryService
from app.services.mcp_server import MCPServerService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user = await UserService(session).create(
        UserCreate(
            username=f"def-{uuid.uuid4().hex[:10]}",
            display_name="Deferred",
            email=f"def-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"def-r-{uuid.uuid4().hex[:8]}", name="Def")
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


@pytest.mark.asyncio
async def test_regular_streamable_http_requires_endpoint(
    db_session: AsyncSession,
) -> None:
    svc = MCPServerService(db_session)
    with pytest.raises(AppError) as exc:
        await svc.create(
            MCPServerCreate(
                name="Needs Endpoint",
                transport_type=MCPTransportType.STREAMABLE_HTTP,
                endpoint_url=None,
            )
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_factory_deferred_draft_allowed_and_gates(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)
    svc = MCPServerService(db_session)
    server = await svc.create_factory_deferred_draft_uncommitted(
        name="Factory Deferred",
        description="deferred",
        transport_config={
            "connection_deferred": True,
            "factory_publish": True,
        },
        code=f"fac-def-{uuid.uuid4().hex[:12]}",
    )
    await db_session.commit()
    assert server.status == MCPServerStatus.DRAFT.value
    assert server.endpoint_url is None
    assert server.transport_config["connection_deferred"] is True

    with pytest.raises(AppError) as act:
        await svc.activate(server.id)
    assert act.value.code == "RESOURCE_CONFLICT"

    discovery = MCPDiscoveryService(db_session, MagicMock())
    with pytest.raises(AppError) as conn:
        await discovery.connection_test(server.id)
    assert conn.value.code == "RESOURCE_CONFLICT"

    checks = (
        await db_session.execute(select(func.count()).select_from(MCPServerCheck))
    ).scalar_one()
    discoveries = (
        await db_session.execute(select(func.count()).select_from(MCPServerDiscovery))
    ).scalar_one()
    assert int(checks) == 0
    assert int(discoveries) == 0
