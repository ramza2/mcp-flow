"""PostgreSQL integration tests for RBAC / ResourceGrant foundation."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.core.errors import AppError
from app.domain.enums import (
    BOOTSTRAP_PERMISSION_CODES,
    ResourceGrantResourceType,
    UserStatus,
)
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.role import PermissionRepository, RoleRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    ResourceGrantCreate,
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
    UserUpdate,
)
from app.services.authorization import AuthorizationResolver, ResourceGrantService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.integration
def test_alembic_rbac_downgrade_upgrade_seed(
    integration_database_url: str,
) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = integration_database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260907_0004")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_permission_seed_present_after_upgrade(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        repo = PermissionRepository(session)
        codes = set()
        page = 1
        while True:
            items, total = await repo.list(page=page, page_size=100)
            codes.update(item.code for item in items)
            if page * 100 >= total:
                break
            page += 1
        assert set(BOOTSTRAP_PERMISSION_CODES).issubset(codes)
        execute = await repo.get_by_code("mcp.tool.execute")
        assert execute is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rbac_db_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    username = f"u-{uuid.uuid4().hex[:8]}"
    role_code = f"r-{uuid.uuid4().hex[:8]}"

    async with integration_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name="U",
            email=f"{username}@example.com",
            status=UserStatus.ACTIVE,
        )
        role = await RoleRepository(session).create(
            code=role_code, name="R", description=None
        )
        await session.commit()
        user_id = user.id
        role_id = role.id

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await UserRepository(session).create(
                username=username,
                display_name="Dup",
                email="dup@example.com",
                status=UserStatus.ACTIVE,
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await RoleRepository(session).create(
                code=role_code, name="Dup", description=None
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, email, status, lock_version) "
                    "VALUES (:id, :username, 'Bad', 'b@x', 'DISABLED', 1)"
                ),
                {"id": uuid.uuid4(), "username": f"bad-{uuid.uuid4().hex[:6]}"},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, email, status, lock_version) "
                    "VALUES (:id, :username, 'Bad', 'b@x', 'ACTIVE', 0)"
                ),
                {"id": uuid.uuid4(), "username": f"lv-{uuid.uuid4().hex[:6]}"},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO roles (id, code, name, lock_version) "
                    "VALUES (:id, :code, 'Bad', 0)"
                ),
                {"id": uuid.uuid4(), "code": f"lv-{uuid.uuid4().hex[:6]}"},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO resource_grants "
                    "(id, user_id, role_id, resource_type, resource_id) "
                    "VALUES (:id, :user_id, :role_id, 'MCP_TOOL', :rid)"
                ),
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "role_id": role_id,
                    "rid": uuid.uuid4(),
                },
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO resource_grants "
                    "(id, user_id, role_id, resource_type, resource_id) "
                    "VALUES (:id, NULL, NULL, 'MCP_TOOL', :rid)"
                ),
                {"id": uuid.uuid4(), "rid": uuid.uuid4()},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO resource_grants "
                    "(id, user_id, resource_type, resource_id) "
                    "VALUES (:id, :user_id, 'ALL', :rid)"
                ),
                {"id": uuid.uuid4(), "user_id": user_id, "rid": uuid.uuid4()},
            )
            await session.commit()
        await session.rollback()

    rid = uuid.uuid4()
    async with integration_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO resource_grants "
                "(id, user_id, resource_type, resource_id) "
                "VALUES (:id, :user_id, 'MCP_TOOL', :rid)"
            ),
            {"id": uuid.uuid4(), "user_id": user_id, "rid": rid},
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO resource_grants "
                    "(id, user_id, resource_type, resource_id) "
                    "VALUES (:id, :user_id, 'MCP_TOOL', :rid)"
                ),
                {"id": uuid.uuid4(), "user_id": user_id, "rid": rid},
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "VALUES (:user_id, :role_id)"
                ),
                {"user_id": user_id, "role_id": role_id},
            )
            await session.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "VALUES (:user_id, :role_id)"
                ),
                {"user_id": user_id, "role_id": role_id},
            )
            await session.commit()
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_user_patch(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user = await UserService(session).create(
            UserCreate(
                username=f"cas-{uuid.uuid4().hex[:8]}",
                display_name="CAS",
                email=f"cas-{uuid.uuid4().hex[:8]}@example.com",
                status=UserStatus.ACTIVE,
            )
        )
        user_id = user.id

    async def _patch(name: str) -> str:
        async with integration_session_factory() as session:
            service = UserService(session)
            current = await service.get(user_id)
            try:
                updated = await service.update(
                    user_id,
                    UserUpdate(display_name=name, lock_version=current.lock_version),
                    expected_lock_version=int(current.lock_version),
                )
                return updated.display_name
            except AppError as exc:
                return f"ERR:{exc.code}"

    results = await asyncio.gather(_patch("A"), _patch("B"))
    successes = [r for r in results if r in {"A", "B"}]
    errors = [r for r in results if r.startswith("ERR:")]
    assert len(successes) == 1
    assert len(errors) == 1
    assert "RESOURCE_VERSION_CONFLICT" in errors[0]

    async with integration_session_factory() as session:
        final = await UserRepository(session).get(user_id)
        assert final is not None
        assert final.lock_version == 2
        assert final.display_name in {"A", "B"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_user_role_replace(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user = await UserService(session).create(
            UserCreate(
                username=f"ur-{uuid.uuid4().hex[:8]}",
                display_name="UR",
                email=f"ur-{uuid.uuid4().hex[:8]}@example.com",
                status=UserStatus.ACTIVE,
            )
        )
        role_a = await RoleService(session).create(
            RoleCreate(code=f"a-{uuid.uuid4().hex[:8]}", name="A")
        )
        role_b = await RoleService(session).create(
            RoleCreate(code=f"b-{uuid.uuid4().hex[:8]}", name="B")
        )
        user_id = user.id
        role_a_id = role_a.id
        role_b_id = role_b.id

    async def _replace(role_id: uuid.UUID) -> str:
        async with integration_session_factory() as session:
            service = UserService(session)
            current = await service.get(user_id)
            try:
                roles = await service.replace_roles(
                    user_id,
                    UserRoleReplaceRequest(role_ids=[role_id]),
                    expected_lock_version=int(current.lock_version),
                )
                return str(roles[0].id)
            except AppError as exc:
                return f"ERR:{exc.code}"

    results = await asyncio.gather(_replace(role_a_id), _replace(role_b_id))
    successes = [r for r in results if not r.startswith("ERR:")]
    errors = [r for r in results if r.startswith("ERR:")]
    assert len(successes) == 1
    assert len(errors) == 1
    assert "RESOURCE_VERSION_CONFLICT" in errors[0]

    async with integration_session_factory() as session:
        final = await UserRepository(session).get(user_id)
        assert final is not None
        assert final.lock_version == 2
        roles = await UserService(session).list_roles(user_id)
        assert len(roles) == 1
        assert roles[0].id in {role_a_id, role_b_id}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_role_permission_replace(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        role = await RoleService(session).create(
            RoleCreate(code=f"rp-{uuid.uuid4().hex[:8]}", name="RP")
        )
        execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
        read = await PermissionRepository(session).get_by_code("mcp.tool.read")
        assert execute is not None and read is not None
        role_id = role.id
        execute_id = execute.id
        read_id = read.id

    async def _replace(permission_id: uuid.UUID) -> str:
        async with integration_session_factory() as session:
            service = RoleService(session)
            current = await service.get(role_id)
            try:
                perms = await service.replace_permissions(
                    role_id,
                    RolePermissionReplaceRequest(permission_ids=[permission_id]),
                    expected_lock_version=int(current.lock_version),
                )
                return str(perms[0].id)
            except AppError as exc:
                return f"ERR:{exc.code}"

    results = await asyncio.gather(_replace(execute_id), _replace(read_id))
    successes = [r for r in results if not r.startswith("ERR:")]
    errors = [r for r in results if r.startswith("ERR:")]
    assert len(successes) == 1
    assert len(errors) == 1
    assert "RESOURCE_VERSION_CONFLICT" in errors[0]

    async with integration_session_factory() as session:
        final = await RoleRepository(session).get(role_id)
        assert final is not None
        assert final.lock_version == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authorization_resolver_postgres_smoke(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user = await UserService(session).create(
            UserCreate(
                username=f"authz-{uuid.uuid4().hex[:8]}",
                display_name="Authz",
                email=f"authz-{uuid.uuid4().hex[:8]}@example.com",
                status=UserStatus.ACTIVE,
            )
        )
        role = await RoleService(session).create(
            RoleCreate(code=f"authz-{uuid.uuid4().hex[:8]}", name="Authz Role")
        )
        execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
        assert execute is not None
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[execute.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=1,
        )
        server = await MCPServerRepository(session).create(
            code=f"srv-{uuid.uuid4().hex[:8]}",
            name="Authz Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool_{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        await ResourceGrantService(session).create_for_user(
            user.id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool.id,
            ),
        )
        user_id = user.id
        tool_id = tool.id

    async with integration_session_factory() as session:
        decision = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert decision.allowed is True
        assert decision.reason_code == "ALLOWED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authorization_revocation_and_soft_deleted_role(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from app.models.auth import Role
    from sqlalchemy import update

    async with integration_session_factory() as session:
        user = await UserService(session).create(
            UserCreate(
                username=f"rev-{uuid.uuid4().hex[:8]}",
                display_name="Rev",
                email=f"rev-{uuid.uuid4().hex[:8]}@example.com",
                status=UserStatus.ACTIVE,
            )
        )
        perm_role = await RoleService(session).create(
            RoleCreate(code=f"perm-{uuid.uuid4().hex[:8]}", name="Perm Role")
        )
        grant_role = await RoleService(session).create(
            RoleCreate(code=f"grant-{uuid.uuid4().hex[:8]}", name="Grant Role")
        )
        execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
        assert execute is not None
        await RoleService(session).replace_permissions(
            perm_role.id,
            RolePermissionReplaceRequest(permission_ids=[execute.id]),
            expected_lock_version=1,
        )
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[perm_role.id, grant_role.id]),
            expected_lock_version=1,
        )
        server = await MCPServerRepository(session).create(
            code=f"srv-{uuid.uuid4().hex[:8]}",
            name="Rev Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool_{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        await ResourceGrantService(session).create_for_role(
            grant_role.id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool.id,
            ),
        )
        user_id = user.id
        tool_id = tool.id
        perm_role_id = perm_role.id
        grant_role_id = grant_role.id
        execute_id = execute.id

    async with integration_session_factory() as session:
        allowed = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert allowed.allowed is True

    async with integration_session_factory() as session:
        role = await RoleService(session).get(perm_role_id)
        await RoleService(session).replace_permissions(
            perm_role_id,
            RolePermissionReplaceRequest(permission_ids=[]),
            expected_lock_version=int(role.lock_version),
        )

    async with integration_session_factory() as session:
        missing_perm = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert missing_perm.allowed is False
        assert missing_perm.reason_code == "PERMISSION_MISSING"

    async with integration_session_factory() as session:
        role = await RoleService(session).get(perm_role_id)
        await RoleService(session).replace_permissions(
            perm_role_id,
            RolePermissionReplaceRequest(permission_ids=[execute_id]),
            expected_lock_version=int(role.lock_version),
        )
        grants = await ResourceGrantService(session).list_for_role(grant_role_id)
        assert grants[1] == 1
        await ResourceGrantService(session).delete_for_role(
            grant_role_id, grants[0][0].id
        )

    async with integration_session_factory() as session:
        missing_grant = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert missing_grant.allowed is False
        assert missing_grant.reason_code == "RESOURCE_GRANT_MISSING"

    # Soft-deleted Role carrying only permission → PERMISSION_MISSING
    async with integration_session_factory() as session:
        other_perm_role = await RoleService(session).create(
            RoleCreate(code=f"live-p-{uuid.uuid4().hex[:8]}", name="Live Perm")
        )
        dead_perm_role = await RoleService(session).create(
            RoleCreate(code=f"dead-p-{uuid.uuid4().hex[:8]}", name="Dead Perm")
        )
        await RoleService(session).replace_permissions(
            dead_perm_role.id,
            RolePermissionReplaceRequest(permission_ids=[execute_id]),
            expected_lock_version=1,
        )
        await ResourceGrantService(session).create_for_user(
            user_id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool_id,
            ),
        )
        user = await UserService(session).get(user_id)
        await UserService(session).replace_roles(
            user_id,
            UserRoleReplaceRequest(role_ids=[dead_perm_role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.execute(
            update(Role)
            .where(Role.id == dead_perm_role.id)
            .values(deleted_at=datetime.now(UTC))
        )
        await session.commit()
        dead_perm_role_id = dead_perm_role.id
        other_perm_role_id = other_perm_role.id

    async with integration_session_factory() as session:
        decision = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert decision.allowed is False
        assert decision.reason_code == "PERMISSION_MISSING"

    # Permission via live Role; grant only on soft-deleted Role → RESOURCE_GRANT_MISSING
    async with integration_session_factory() as session:
        await RoleService(session).replace_permissions(
            other_perm_role_id,
            RolePermissionReplaceRequest(permission_ids=[execute_id]),
            expected_lock_version=1,
        )
        dead_grant_role = await RoleService(session).create(
            RoleCreate(code=f"dead-g-{uuid.uuid4().hex[:8]}", name="Dead Grant")
        )
        # clear direct user grants
        user_grants, _ = await ResourceGrantService(session).list_for_user(user_id)
        for grant in user_grants:
            await ResourceGrantService(session).delete_for_user(user_id, grant.id)
        await ResourceGrantService(session).create_for_role(
            dead_grant_role.id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool_id,
            ),
        )
        user = await UserService(session).get(user_id)
        await UserService(session).replace_roles(
            user_id,
            UserRoleReplaceRequest(
                role_ids=[other_perm_role_id, dead_grant_role.id]
            ),
            expected_lock_version=int(user.lock_version),
        )
        await session.execute(
            update(Role)
            .where(Role.id == dead_grant_role.id)
            .values(deleted_at=datetime.now(UTC))
        )
        await session.commit()

    async with integration_session_factory() as session:
        decision = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert decision.allowed is False
        assert decision.reason_code == "RESOURCE_GRANT_MISSING"

    # Soft-deleted user → USER_NOT_FOUND
    async with integration_session_factory() as session:
        from app.models.auth import User

        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(deleted_at=datetime.now(UTC))
        )
        await session.commit()

    async with integration_session_factory() as session:
        decision = await AuthorizationResolver(session).authorize_resource(
            user_id, "mcp.tool.execute", "MCP_TOOL", tool_id
        )
        assert decision.allowed is False
        assert decision.reason_code == "USER_NOT_FOUND"

    # silence unused in soft-delete-only-permission path
    assert dead_perm_role_id is not None
