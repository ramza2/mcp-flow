"""SQLite-backed API tests for User / Role / Permission / ResourceGrant."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.domain.enums import BOOTSTRAP_PERMISSION_CODES
from app.services.authorization import AuthorizationResolver
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

USERS = "/api/v1/users"
ROLES = "/api/v1/roles"
PERMS = "/api/v1/permissions"
AGENTS = "/api/v1/agents"


def _user_body(**overrides: Any) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "username": f"user_{suffix}",
        "display_name": f"User {suffix}",
        "email": f"user_{suffix}@example.com",
        "status": "ACTIVE",
    }
    payload.update(overrides)
    return payload


def _role_body(**overrides: Any) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "code": f"role_{suffix}",
        "name": f"Role {suffix}",
        "description": "test role",
    }
    payload.update(overrides)
    return payload


async def _create_user(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post(USERS, json=_user_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


async def _create_role(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post(ROLES, json=_role_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


async def _permission_id(client: AsyncClient, code: str) -> str:
    listing = await client.get(PERMS, params={"page_size": 100, "q": code})
    assert listing.status_code == 200, listing.text
    for item in listing.json()["items"]:
        if item["code"] == code:
            return item["id"]
    raise AssertionError(f"permission {code} not found")


async def _seed_mcp_tool(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"srv-{uuid.uuid4().hex[:8]}",
            name="Auth Tool Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool_{uuid.uuid4().hex[:6]}",
            status="ACTIVE",
        )
        await session.commit()
        return tool.id, server.id


async def _seed_agent(client: AsyncClient) -> str:
    response = await client.post(
        AGENTS,
        json={"name": "Grant Agent", "description": "g", "visibility": "PRIVATE"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_user_crud_statuses_and_injections(db_client: AsyncClient) -> None:
    created = await _create_user(db_client, status="ACTIVE")
    assert created["lock_version"] == 1
    assert created["status"] == "ACTIVE"
    assert "password_hash" not in created
    assert created.get("last_login_at") is None
    user_id = created["id"]

    detail = await db_client.get(f"{USERS}/{user_id}")
    assert detail.status_code == 200
    assert "password_hash" not in detail.json()

    listing = await db_client.get(USERS, params={"q": created["username"]})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    current = detail.json()
    for status_value in ("INACTIVE", "LOCKED", "ACTIVE"):
        patched = await db_client.patch(
            f"{USERS}/{user_id}",
            headers={"If-Match": str(current["lock_version"])},
            json={"status": status_value, "lock_version": current["lock_version"]},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == status_value
        current = patched.json()

    unknown_status = await db_client.post(
        USERS, json=_user_body(status="DISABLED")
    )
    assert unknown_status.status_code == 422

    unknown_field = await db_client.patch(
        f"{USERS}/{user_id}",
        headers={"If-Match": str(current["lock_version"])},
        json={"display_name": "x", "unknown_field": True},
    )
    assert unknown_field.status_code == 422

    password_inject = await db_client.post(
        USERS, json=_user_body(password="secret")
    )
    assert password_inject.status_code == 422

    password_hash_inject = await db_client.post(
        USERS, json=_user_body(password_hash="x")
    )
    assert password_hash_inject.status_code == 422

    username_patch = await db_client.patch(
        f"{USERS}/{user_id}",
        headers={"If-Match": str(current["lock_version"])},
        json={"username": "hijack"},
    )
    assert username_patch.status_code == 422

    stale = await db_client.patch(
        f"{USERS}/{user_id}",
        headers={"If-Match": "1"},
        json={"display_name": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    missing = await db_client.patch(
        f"{USERS}/{uuid.uuid4()}",
        headers={"If-Match": "1"},
        json={"display_name": "gone"},
    )
    assert missing.status_code == 404

    dup = await db_client.post(
        USERS, json=_user_body(username=created["username"])
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_role_crud_code_unique_and_patch_rules(db_client: AsyncClient) -> None:
    created = await _create_role(db_client)
    role_id = created["id"]
    assert created["lock_version"] == 1
    assert created["code"]

    listing = await db_client.get(ROLES, params={"q": created["code"]})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    detail = await db_client.get(f"{ROLES}/{role_id}")
    assert detail.status_code == 200

    patched = await db_client.patch(
        f"{ROLES}/{role_id}",
        headers={"If-Match": "1"},
        json={"name": "Renamed", "description": "updated"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["lock_version"] == 2
    assert patched.json()["code"] == created["code"]

    code_patch = await db_client.patch(
        f"{ROLES}/{role_id}",
        headers={"If-Match": "2"},
        json={"code": "hijack"},
    )
    assert code_patch.status_code == 422

    unknown = await db_client.patch(
        f"{ROLES}/{role_id}",
        headers={"If-Match": "2"},
        json={"name": "x", "unknown_field": True},
    )
    assert unknown.status_code == 422

    stale = await db_client.patch(
        f"{ROLES}/{role_id}",
        headers={"If-Match": "1"},
        json={"name": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    dup = await db_client.post(ROLES, json=_role_body(code=created["code"]))
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_permission_catalog_bootstrap_and_read_only(
    db_client: AsyncClient,
) -> None:
    listing = await db_client.get(PERMS, params={"page_size": 100, "sort": "code"})
    assert listing.status_code == 200
    codes = {item["code"] for item in listing.json()["items"]}
    assert "mcp.tool.execute" in codes
    assert set(BOOTSTRAP_PERMISSION_CODES).issubset(codes)
    assert len(codes) == len(set(codes))

    assert (await db_client.post(PERMS, json={"code": "x"})).status_code in {404, 405}
    first = listing.json()["items"][0]
    assert (
        await db_client.patch(f"{PERMS}/{first['id']}", json={"name": "x"})
    ).status_code in {404, 405}


@pytest.mark.asyncio
async def test_user_role_replace_semantics(db_client: AsyncClient) -> None:
    user = await _create_user(db_client)
    role_a = await _create_role(db_client, code=f"a-{uuid.uuid4().hex[:6]}")
    role_b = await _create_role(db_client, code=f"b-{uuid.uuid4().hex[:6]}")
    user_id = user["id"]

    put_ab = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "1"},
        json={"role_ids": [role_a["id"], role_b["id"]]},
    )
    assert put_ab.status_code == 200, put_ab.text
    assert {r["id"] for r in put_ab.json()} == {role_a["id"], role_b["id"]}
    user = (await db_client.get(f"{USERS}/{user_id}")).json()
    assert user["lock_version"] == 2

    put_b = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "2"},
        json={"role_ids": [role_b["id"]]},
    )
    assert put_b.status_code == 200
    assert [r["id"] for r in put_b.json()] == [role_b["id"]]
    user = (await db_client.get(f"{USERS}/{user_id}")).json()
    assert user["lock_version"] == 3

    noop = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "3"},
        json={"role_ids": [role_b["id"]]},
    )
    assert noop.status_code == 200
    user = (await db_client.get(f"{USERS}/{user_id}")).json()
    assert user["lock_version"] == 3

    reorder = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "3"},
        json={"role_ids": [role_b["id"], role_a["id"]]},
    )
    # Set differs from [B] — this is a change to {A,B}
    assert reorder.status_code == 200
    user = (await db_client.get(f"{USERS}/{user_id}")).json()
    assert user["lock_version"] == 4

    same_set = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "4"},
        json={"role_ids": [role_a["id"], role_b["id"]]},
    )
    assert same_set.status_code == 200
    user = (await db_client.get(f"{USERS}/{user_id}")).json()
    assert user["lock_version"] == 4

    reordered_same = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "4"},
        json={"role_ids": [role_b["id"], role_a["id"]]},
    )
    assert reordered_same.status_code == 200
    user = (await db_client.get(f"{USERS}/{user_id}")).json()
    assert user["lock_version"] == 4

    stale = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "1"},
        json={"role_ids": [role_a["id"]]},
    )
    assert stale.status_code == 409

    before = (await db_client.get(f"{USERS}/{user_id}/roles")).json()
    missing = await db_client.put(
        f"{USERS}/{user_id}/roles",
        headers={"If-Match": "4"},
        json={"role_ids": [str(uuid.uuid4())]},
    )
    assert missing.status_code == 422
    after = (await db_client.get(f"{USERS}/{user_id}/roles")).json()
    assert {r["id"] for r in after} == {r["id"] for r in before}
    user = (await db_client.get(f"{USERS}/{user_id}")).json()
    assert user["lock_version"] == 4


@pytest.mark.asyncio
async def test_role_permission_replace_semantics(db_client: AsyncClient) -> None:
    role = await _create_role(db_client)
    role_id = role["id"]
    execute_id = await _permission_id(db_client, "mcp.tool.execute")
    read_id = await _permission_id(db_client, "mcp.tool.read")

    put_both = await db_client.put(
        f"{ROLES}/{role_id}/permissions",
        headers={"If-Match": "1"},
        json={"permission_ids": [execute_id, read_id]},
    )
    assert put_both.status_code == 200, put_both.text
    assert {p["id"] for p in put_both.json()} == {execute_id, read_id}
    role = (await db_client.get(f"{ROLES}/{role_id}")).json()
    assert role["lock_version"] == 2

    put_one = await db_client.put(
        f"{ROLES}/{role_id}/permissions",
        headers={"If-Match": "2"},
        json={"permission_ids": [execute_id]},
    )
    assert put_one.status_code == 200
    role = (await db_client.get(f"{ROLES}/{role_id}")).json()
    assert role["lock_version"] == 3

    noop = await db_client.put(
        f"{ROLES}/{role_id}/permissions",
        headers={"If-Match": "3"},
        json={"permission_ids": [execute_id]},
    )
    assert noop.status_code == 200
    role = (await db_client.get(f"{ROLES}/{role_id}")).json()
    assert role["lock_version"] == 3

    reorder_noop = await db_client.put(
        f"{ROLES}/{role_id}/permissions",
        headers={"If-Match": "3"},
        json={"permission_ids": [read_id, execute_id]},
    )
    assert reorder_noop.status_code == 200
    role = (await db_client.get(f"{ROLES}/{role_id}")).json()
    assert role["lock_version"] == 4

    same_set = await db_client.put(
        f"{ROLES}/{role_id}/permissions",
        headers={"If-Match": "4"},
        json={"permission_ids": [execute_id, read_id]},
    )
    assert same_set.status_code == 200
    role = (await db_client.get(f"{ROLES}/{role_id}")).json()
    assert role["lock_version"] == 4

    stale = await db_client.put(
        f"{ROLES}/{role_id}/permissions",
        headers={"If-Match": "1"},
        json={"permission_ids": [execute_id]},
    )
    assert stale.status_code == 409

    before = (await db_client.get(f"{ROLES}/{role_id}/permissions")).json()
    missing = await db_client.put(
        f"{ROLES}/{role_id}/permissions",
        headers={"If-Match": "4"},
        json={"permission_ids": [str(uuid.uuid4())]},
    )
    assert missing.status_code == 422
    after = (await db_client.get(f"{ROLES}/{role_id}/permissions")).json()
    assert {p["id"] for p in after} == {p["id"] for p in before}


@pytest.mark.asyncio
async def test_user_resource_grant_crud(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await _create_user(db_client)
    other = await _create_user(db_client)
    tool_id, _server_id = await _seed_mcp_tool(db_session_factory)

    created = await db_client.post(
        f"{USERS}/{user['id']}/resource-grants",
        json={"resource_type": "MCP_TOOL", "resource_id": str(tool_id)},
    )
    assert created.status_code == 201, created.text
    grant_id = created.json()["id"]
    assert created.json()["user_id"] == user["id"]
    assert created.json()["role_id"] is None

    listing = await db_client.get(f"{USERS}/{user['id']}/resource-grants")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    dup = await db_client.post(
        f"{USERS}/{user['id']}/resource-grants",
        json={"resource_type": "MCP_TOOL", "resource_id": str(tool_id)},
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "RESOURCE_CONFLICT"

    wrong = await db_client.delete(
        f"{USERS}/{other['id']}/resource-grants/{grant_id}"
    )
    assert wrong.status_code == 404

    deleted = await db_client.delete(
        f"{USERS}/{user['id']}/resource-grants/{grant_id}"
    )
    assert deleted.status_code == 204
    listing = await db_client.get(f"{USERS}/{user['id']}/resource-grants")
    assert listing.json()["total"] == 0


@pytest.mark.asyncio
async def test_role_resource_grant_crud(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    role = await _create_role(db_client)
    tool_id, _ = await _seed_mcp_tool(db_session_factory)
    created = await db_client.post(
        f"{ROLES}/{role['id']}/resource-grants",
        json={"resource_type": "MCP_TOOL", "resource_id": str(tool_id)},
    )
    assert created.status_code == 201, created.text
    grant_id = created.json()["id"]
    assert created.json()["role_id"] == role["id"]
    assert created.json()["user_id"] is None

    listing = await db_client.get(f"{ROLES}/{role['id']}/resource-grants")
    assert listing.json()["total"] == 1

    deleted = await db_client.delete(
        f"{ROLES}/{role['id']}/resource-grants/{grant_id}"
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_resource_grant_existence_and_workflow_fail_closed(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await _create_user(db_client)
    missing = str(uuid.uuid4())

    for resource_type in ("AGENT", "MCP_SERVER", "MCP_TOOL"):
        response = await db_client.post(
            f"{USERS}/{user['id']}/resource-grants",
            json={"resource_type": resource_type, "resource_id": missing},
        )
        assert response.status_code == 422, response.text

    workflow = await db_client.post(
        f"{USERS}/{user['id']}/resource-grants",
        json={"resource_type": "WORKFLOW", "resource_id": missing},
    )
    assert workflow.status_code == 409
    assert "not implemented" in workflow.json()["error"]["message"].lower()

    agent_id = await _seed_agent(db_client)
    tool_id, server_id = await _seed_mcp_tool(db_session_factory)
    for resource_type, resource_id in (
        ("AGENT", agent_id),
        ("MCP_SERVER", str(server_id)),
        ("MCP_TOOL", str(tool_id)),
    ):
        ok = await db_client.post(
            f"{USERS}/{user['id']}/resource-grants",
            json={"resource_type": resource_type, "resource_id": resource_id},
        )
        assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_authorization_resolver_matrix(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _create_user(db_client)
    role = await _create_role(db_client)
    execute_id = await _permission_id(db_client, "mcp.tool.execute")
    tool_id, server_id = await _seed_mcp_tool(db_session_factory)
    other_tool = (await _seed_mcp_tool(db_session_factory))[0]

    await db_client.put(
        f"{ROLES}/{role['id']}/permissions",
        headers={"If-Match": "1"},
        json={"permission_ids": [execute_id]},
    )
    await db_client.put(
        f"{USERS}/{user['id']}/roles",
        headers={"If-Match": "1"},
        json={"role_ids": [role["id"]]},
    )
    await db_client.post(
        f"{USERS}/{user['id']}/resource-grants",
        json={"resource_type": "MCP_TOOL", "resource_id": str(tool_id)},
    )

    async with db_session_factory() as session:
        resolver = AuthorizationResolver(session)
        allowed = await resolver.authorize_resource(
            uuid.UUID(user["id"]),
            "mcp.tool.execute",
            "MCP_TOOL",
            tool_id,
        )
        assert allowed.allowed is True
        assert allowed.reason_code == "ALLOWED"

        perm_only = await resolver.authorize_resource(
            uuid.UUID(user["id"]),
            "mcp.tool.execute",
            "MCP_TOOL",
            other_tool,
        )
        assert perm_only.allowed is False
        assert perm_only.reason_code == "RESOURCE_GRANT_MISSING"

        server_grant = await resolver.authorize_resource(
            uuid.UUID(user["id"]),
            "mcp.tool.execute",
            "MCP_TOOL",
            tool_id,
        )
        assert server_grant.allowed is True

    # Replace direct grant with MCP_SERVER only — tool authorize must deny
    grants = (
        await db_client.get(f"{USERS}/{user['id']}/resource-grants")
    ).json()["items"]
    for grant in grants:
        await db_client.delete(
            f"{USERS}/{user['id']}/resource-grants/{grant['id']}"
        )
    await db_client.post(
        f"{USERS}/{user['id']}/resource-grants",
        json={"resource_type": "MCP_SERVER", "resource_id": str(server_id)},
    )
    async with db_session_factory() as session:
        resolver = AuthorizationResolver(session)
        decision = await resolver.authorize_resource(
            uuid.UUID(user["id"]),
            "mcp.tool.execute",
            "MCP_TOOL",
            tool_id,
        )
        assert decision.allowed is False
        assert decision.reason_code == "RESOURCE_GRANT_MISSING"

    # Grant-only: remove permission from role
    role = (await db_client.get(f"{ROLES}/{role['id']}")).json()
    await db_client.put(
        f"{ROLES}/{role['id']}/permissions",
        headers={"If-Match": str(role["lock_version"])},
        json={"permission_ids": []},
    )
    await db_client.post(
        f"{USERS}/{user['id']}/resource-grants",
        json={"resource_type": "MCP_TOOL", "resource_id": str(tool_id)},
    )
    async with db_session_factory() as session:
        resolver = AuthorizationResolver(session)
        decision = await resolver.authorize_resource(
            uuid.UUID(user["id"]),
            "mcp.tool.execute",
            "MCP_TOOL",
            tool_id,
        )
        assert decision.allowed is False
        assert decision.reason_code == "PERMISSION_MISSING"

    # Restore permission + role grant path
    role = (await db_client.get(f"{ROLES}/{role['id']}")).json()
    await db_client.put(
        f"{ROLES}/{role['id']}/permissions",
        headers={"If-Match": str(role["lock_version"])},
        json={"permission_ids": [execute_id]},
    )
    # clear user grants; add role grant
    grants = (
        await db_client.get(f"{USERS}/{user['id']}/resource-grants")
    ).json()["items"]
    for grant in grants:
        await db_client.delete(
            f"{USERS}/{user['id']}/resource-grants/{grant['id']}"
        )
    await db_client.post(
        f"{ROLES}/{role['id']}/resource-grants",
        json={"resource_type": "MCP_TOOL", "resource_id": str(tool_id)},
    )
    async with db_session_factory() as session:
        resolver = AuthorizationResolver(session)
        decision = await resolver.authorize_resource(
            uuid.UUID(user["id"]),
            "mcp.tool.execute",
            "MCP_TOOL",
            tool_id,
        )
        assert decision.allowed is True

    # Inactive / Locked deny even with both
    user = (await db_client.get(f"{USERS}/{user['id']}")).json()
    for status_value in ("INACTIVE", "LOCKED"):
        patched = await db_client.patch(
            f"{USERS}/{user['id']}",
            headers={"If-Match": str(user["lock_version"])},
            json={"status": status_value},
        )
        assert patched.status_code == 200
        user = patched.json()
        async with db_session_factory() as session:
            resolver = AuthorizationResolver(session)
            decision = await resolver.authorize_resource(
                uuid.UUID(user["id"]),
                "mcp.tool.execute",
                "MCP_TOOL",
                tool_id,
            )
            assert decision.allowed is False
            assert decision.reason_code == "USER_NOT_ACTIVE"

    # Membership removal immediate deny
    patched = await db_client.patch(
        f"{USERS}/{user['id']}",
        headers={"If-Match": str(user["lock_version"])},
        json={"status": "ACTIVE"},
    )
    user = patched.json()
    await db_client.put(
        f"{USERS}/{user['id']}/roles",
        headers={"If-Match": str(user["lock_version"])},
        json={"role_ids": []},
    )
    async with db_session_factory() as session:
        resolver = AuthorizationResolver(session)
        decision = await resolver.authorize_resource(
            uuid.UUID(user["id"]),
            "mcp.tool.execute",
            "MCP_TOOL",
            tool_id,
        )
        assert decision.allowed is False
        assert decision.reason_code == "PERMISSION_MISSING"
