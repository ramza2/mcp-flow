"""API tests for MCP Tool lifecycle, policy, and verification slice."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.domain.enums import ApprovalPolicyStatus, MCPToolStatus, ToolVersionValidationStatus
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.mcp_tool import MCPToolRepository
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.test_mcp_server import TestMCPScenario

API_SERVERS = "/api/v1/mcp/servers"
API_TOOLS = "/api/v1/mcp/tools"


async def _create_server(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        API_SERVERS,
        json={
            "name": "Policy Slice MCP",
            "transport_type": "STREAMABLE_HTTP",
            "endpoint_url": "https://mcp.test/mcp",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _discover_tools(
    client: AsyncClient,
    override_mcp_client,
    *,
    scenario: str = TestMCPScenario.HEALTHY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    server = await _create_server(client)
    override_mcp_client(TestMCPScenario(scenario).build_http_client())
    check = await client.post(f"{API_SERVERS}/{server['id']}/connection-tests")
    assert check.status_code == 201, check.text
    discovery = await client.post(
        f"{API_SERVERS}/{server['id']}/discoveries",
        json={"apply_changes": True},
    )
    assert discovery.status_code == 201, discovery.text
    assert discovery.json()["success"] is True
    tools = await client.get(API_TOOLS, params={"mcp_server_id": server["id"]})
    assert tools.status_code == 200
    items = tools.json()["items"]
    assert items
    return server, items[0]


async def _create_approval_policy(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str = ApprovalPolicyStatus.ACTIVE,
) -> uuid.UUID:
    async with session_factory() as session:
        row = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Default Approval",
            status=status,
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
        )
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_tool_patch_metadata_and_lock(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]

    patched = await db_client.patch(
        f"{API_TOOLS}/{tool_id}",
        headers={"If-Match": str(tool["lock_version"])},
        json={
            "display_name": "Echo Friendly",
            "description_override": "Operator description",
            "tags": ["ops", "ops", "  ", "search"],
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["display_name"] == "Echo Friendly"
    assert body["description_override"] == "Operator description"
    assert body["tags"] == ["ops", "search"]
    assert body["lock_version"] == tool["lock_version"] + 1

    stale = await db_client.patch(
        f"{API_TOOLS}/{tool_id}",
        headers={"If-Match": str(tool["lock_version"])},
        json={"display_name": "Stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_tool_patch_rejects_unknown_and_status_fields(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    for payload in (
        {"status": "ACTIVE", "lock_version": tool["lock_version"]},
        {"remote_name": "hacked", "lock_version": tool["lock_version"]},
        {"current_version_id": str(uuid.uuid4()), "lock_version": tool["lock_version"]},
    ):
        response = await db_client.patch(
            f"{API_TOOLS}/{tool['id']}",
            headers={"If-Match": str(tool["lock_version"])},
            json=payload,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_tool_patch_not_found(db_client: AsyncClient) -> None:
    response = await db_client.patch(
        f"{API_TOOLS}/{uuid.uuid4()}",
        headers={"If-Match": "1"},
        json={"display_name": "x"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_tool_activate_deactivate_lifecycle(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    assert tool["status"] == MCPToolStatus.DISCOVERED
    lock = int(tool["lock_version"])

    activated = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": str(lock)},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == MCPToolStatus.ACTIVE
    lock = int(activated.json()["lock_version"])
    assert lock == tool["lock_version"] + 1

    again = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": str(lock)},
    )
    assert again.status_code == 200
    assert again.json()["status"] == MCPToolStatus.ACTIVE
    assert again.json()["lock_version"] == lock

    deactivated = await db_client.post(
        f"{API_TOOLS}/{tool_id}/deactivate",
        headers={"If-Match": str(lock)},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == MCPToolStatus.INACTIVE
    lock = int(deactivated.json()["lock_version"])

    reactivated = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": str(lock)},
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["status"] == MCPToolStatus.ACTIVE
    lock = int(reactivated.json()["lock_version"])

    idle = await db_client.post(
        f"{API_TOOLS}/{tool_id}/deactivate",
        headers={"If-Match": str(lock)},
    )
    assert idle.status_code == 200
    lock = int(idle.json()["lock_version"])
    idle2 = await db_client.post(
        f"{API_TOOLS}/{tool_id}/deactivate",
        headers={"If-Match": str(lock)},
    )
    assert idle2.status_code == 200
    assert idle2.json()["status"] == MCPToolStatus.INACTIVE
    assert idle2.json()["lock_version"] == lock


@pytest.mark.asyncio
async def test_tool_activate_stale_if_match_conflict(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    lock = int(tool["lock_version"])
    assert lock == 1

    activated = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": "1"},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == MCPToolStatus.ACTIVE
    assert activated.json()["lock_version"] == 2

    stale = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": "1"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_tool_deactivate_requires_if_match(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    activated = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": str(tool["lock_version"])},
    )
    assert activated.status_code == 200

    missing = await db_client.post(f"{API_TOOLS}/{tool_id}/deactivate")
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"

    bad = await db_client.post(
        f"{API_TOOLS}/{tool_id}/deactivate",
        headers={"If-Match": "not-an-int"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_tool_missing_blocked_activate_conflict(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = uuid.UUID(tool["id"])

    async with db_session_factory() as session:
        row = await MCPToolRepository(session).get(tool_id)
        assert row is not None
        updated = await MCPToolRepository(session).update_atomic(
            tool_id,
            expected_lock_version=int(row.lock_version),
            status=MCPToolStatus.MISSING,
        )
        assert updated is not None
        await session.commit()
        lock = int(updated.lock_version)

    conflict = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": str(lock)},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RESOURCE_CONFLICT"

    preserved = await db_client.post(
        f"{API_TOOLS}/{tool_id}/deactivate",
        headers={"If-Match": str(lock)},
    )
    assert preserved.status_code == 409
    detail = await db_client.get(f"{API_TOOLS}/{tool_id}")
    assert detail.json()["status"] == MCPToolStatus.MISSING

    async with db_session_factory() as session:
        row = await MCPToolRepository(session).get(tool_id)
        assert row is not None
        updated = await MCPToolRepository(session).update_atomic(
            tool_id,
            expected_lock_version=int(row.lock_version),
            status=MCPToolStatus.BLOCKED,
        )
        assert updated is not None
        await session.commit()
        lock = int(updated.lock_version)

    blocked_act = await db_client.post(
        f"{API_TOOLS}/{tool_id}/activate",
        headers={"If-Match": str(lock)},
    )
    assert blocked_act.status_code == 409
    blocked_deact = await db_client.post(
        f"{API_TOOLS}/{tool_id}/deactivate",
        headers={"If-Match": str(lock)},
    )
    assert blocked_deact.status_code == 409
    assert (await db_client.get(f"{API_TOOLS}/{tool_id}")).json()["status"] == MCPToolStatus.BLOCKED


@pytest.mark.asyncio
async def test_discovered_deactivate_conflict(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    response = await db_client.post(
        f"{API_TOOLS}/{tool['id']}/deactivate",
        headers={"If-Match": str(tool["lock_version"])},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_tool_policy_create_get_update_and_validation(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    approval_id = await _create_approval_policy(db_session_factory)

    missing = await db_client.get(f"{API_TOOLS}/{tool_id}/policy")
    assert missing.status_code == 404

    created = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        json={
            "risk_class": "READ_ONLY",
            "requires_confirmation": False,
            "requires_approval": False,
            "timeout_ms": 15000,
            "max_attempts": 2,
            "max_result_bytes": 1024,
            "allow_auto_select": True,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["risk_class"] == "READ_ONLY"
    assert created.json()["lock_version"] == 1

    got = await db_client.get(f"{API_TOOLS}/{tool_id}/policy")
    assert got.status_code == 200
    assert got.json()["id"] == created.json()["id"]

    updated = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        headers={"If-Match": "1"},
        json={
            "risk_class": "NON_IDEMPOTENT_WRITE",
            "requires_confirmation": True,
            "requires_approval": True,
            "approval_policy_id": str(approval_id),
            "timeout_ms": 30000,
            "max_attempts": 1,
            "max_result_bytes": 2048,
            "allow_auto_select": False,
            "lock_version": 1,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["lock_version"] == 2
    assert updated.json()["approval_policy_id"] == str(approval_id)

    stale = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        headers={"If-Match": "1"},
        json={
            "risk_class": "READ_ONLY",
            "timeout_ms": 1000,
            "max_attempts": 1,
            "max_result_bytes": 100,
            "lock_version": 1,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    bad_risk = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        headers={"If-Match": "2"},
        json={
            "risk_class": "WRITE",
            "timeout_ms": 1000,
            "max_attempts": 1,
            "max_result_bytes": 100,
            "lock_version": 2,
        },
    )
    assert bad_risk.status_code == 422

    for payload in (
        {"risk_class": "READ_ONLY", "timeout_ms": 0, "max_attempts": 1, "max_result_bytes": 10},
        {"risk_class": "READ_ONLY", "timeout_ms": 10, "max_attempts": 0, "max_result_bytes": 10},
        {"risk_class": "READ_ONLY", "timeout_ms": 10, "max_attempts": 1, "max_result_bytes": 0},
    ):
        response = await db_client.put(
            f"{API_TOOLS}/{tool_id}/policy",
            headers={"If-Match": "2"},
            json={**payload, "lock_version": 2},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_tool_policy_approval_policy_rules(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    active_id = await _create_approval_policy(db_session_factory)
    inactive_id = await _create_approval_policy(
        db_session_factory,
        status=ApprovalPolicyStatus.INACTIVE,
    )

    no_id = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        json={
            "risk_class": "DESTRUCTIVE",
            "requires_approval": True,
            "timeout_ms": 1000,
            "max_attempts": 1,
            "max_result_bytes": 100,
        },
    )
    assert no_id.status_code == 422

    missing = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        json={
            "risk_class": "DESTRUCTIVE",
            "requires_approval": True,
            "approval_policy_id": str(uuid.uuid4()),
            "timeout_ms": 1000,
            "max_attempts": 1,
            "max_result_bytes": 100,
        },
    )
    assert missing.status_code == 422

    inactive = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        json={
            "risk_class": "DESTRUCTIVE",
            "requires_approval": True,
            "approval_policy_id": str(inactive_id),
            "timeout_ms": 1000,
            "max_attempts": 1,
            "max_result_bytes": 100,
        },
    )
    assert inactive.status_code == 422

    unexpected = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        json={
            "risk_class": "READ_ONLY",
            "requires_approval": False,
            "approval_policy_id": str(active_id),
            "timeout_ms": 1000,
            "max_attempts": 1,
            "max_result_bytes": 100,
        },
    )
    assert unexpected.status_code == 422

    ok = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        json={
            "risk_class": "DESTRUCTIVE",
            "requires_approval": True,
            "approval_policy_id": str(active_id),
            "timeout_ms": 1000,
            "max_attempts": 1,
            "max_result_bytes": 100,
        },
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_verification_pending_failed_list_detail(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    version_id = tool["current_version_id"]

    pending = await db_client.post(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications",
        json={"status": "PENDING", "criteria_version": "tool-verification-v1"},
    )
    assert pending.status_code == 201, pending.text
    assert pending.json()["status"] == "PENDING"
    assert pending.json()["verified_by"] is None

    failed = await db_client.post(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications",
        json={
            "status": "FAILED",
            "criteria_version": "tool-verification-v1",
            "result_summary": {"schema_valid": False, "reason": "timeout"},
        },
    )
    assert failed.status_code == 201

    listing = await db_client.get(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications"
    )
    assert listing.status_code == 200
    assert listing.json()["total"] >= 2

    detail = await db_client.get(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications/{pending.json()['id']}"
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == pending.json()["id"]

    wrong_tool = await db_client.get(
        f"{API_TOOLS}/{uuid.uuid4()}/versions/{version_id}/verifications/{pending.json()['id']}"
    )
    assert wrong_tool.status_code == 404

    wrong_version = await db_client.get(
        f"{API_TOOLS}/{tool_id}/versions/{uuid.uuid4()}/verifications/{pending.json()['id']}"
    )
    assert wrong_version.status_code == 404


@pytest.mark.asyncio
async def test_verified_preconditions_and_success(
    db_client: AsyncClient,
    override_mcp_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    version_id = tool["current_version_id"]

    # INVALID version reject
    async with db_session_factory() as session:
        version = await MCPToolRepository(session).get_version(uuid.UUID(version_id))
        assert version is not None
        version.validation_status = ToolVersionValidationStatus.INVALID
        await session.commit()

    invalid = await db_client.post(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications",
        json={
            "status": "VERIFIED",
            "criteria_version": "tool-verification-v1",
            "test_execution_id": str(uuid.uuid4()),
            "evidence_blob_id": str(uuid.uuid4()),
            "result_summary": {
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
            },
        },
    )
    assert invalid.status_code == 409

    async with db_session_factory() as session:
        version = await MCPToolRepository(session).get_version(uuid.UUID(version_id))
        assert version is not None
        version.validation_status = ToolVersionValidationStatus.VALID
        await session.commit()

    # Missing evidence fields
    missing_exec = await db_client.post(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications",
        json={
            "status": "VERIFIED",
            "criteria_version": "tool-verification-v1",
            "evidence_blob_id": str(uuid.uuid4()),
            "result_summary": {
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
            },
        },
    )
    assert missing_exec.status_code == 422

    # Happy path — connection check + successful discovery already exist from helper
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    verified = await db_client.post(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications",
        json={
            "status": "VERIFIED",
            "criteria_version": "tool-verification-v1",
            "test_execution_id": str(uuid.uuid4()),
            "evidence_blob_id": str(uuid.uuid4()),
            "expires_at": expires,
            "result_summary": {
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
                "extra_note": "allowed",
            },
        },
    )
    assert verified.status_code == 201, verified.text
    assert verified.json()["status"] == "VERIFIED"
    assert verified.json()["verified_by"] is None

    # Past expires_at rejected
    past = await db_client.post(
        f"{API_TOOLS}/{tool_id}/versions/{version_id}/verifications",
        json={
            "status": "VERIFIED",
            "criteria_version": "tool-verification-v1",
            "test_execution_id": str(uuid.uuid4()),
            "evidence_blob_id": str(uuid.uuid4()),
            "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "result_summary": {
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
            },
        },
    )
    assert past.status_code == 422

    # No successful check path
    async with db_session_factory() as session:
        # Create a fresh tool version on same server but wipe checks by using a new server tool
        # Simpler: mark that has_succeeded_check is required — create tool without checks via repo
        tools = MCPToolRepository(session)
        fresh = await tools.create_tool(
            mcp_server_id=uuid.UUID(server["id"]),
            remote_name=f"orphan-{uuid.uuid4().hex[:6]}",
        )
        version = await tools.create_version(
            mcp_tool_id=fresh.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status=ToolVersionValidationStatus.VALID,
        )
        fresh.current_version_id = version.id
        await session.commit()
        orphan_tool_id = fresh.id
        orphan_version_id = version.id

    # Server still has succeeded checks from earlier CT, so VERIFIED can still pass check
    # precondition. To force failure, use a brand-new server without checks.
    new_server = await _create_server(db_client)
    async with db_session_factory() as session:
        tools = MCPToolRepository(session)
        fresh = await tools.create_tool(
            mcp_server_id=uuid.UUID(new_server["id"]),
            remote_name="lonely",
        )
        version = await tools.create_version(
            mcp_tool_id=fresh.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status=ToolVersionValidationStatus.VALID,
        )
        fresh.current_version_id = version.id
        await session.commit()
        lonely_tool = fresh.id
        lonely_version = version.id

    no_check = await db_client.post(
        f"{API_TOOLS}/{lonely_tool}/versions/{lonely_version}/verifications",
        json={
            "status": "VERIFIED",
            "criteria_version": "tool-verification-v1",
            "test_execution_id": str(uuid.uuid4()),
            "evidence_blob_id": str(uuid.uuid4()),
            "result_summary": {
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
            },
        },
    )
    assert no_check.status_code == 409

    # unused vars silence
    assert orphan_tool_id and orphan_version_id


@pytest.mark.asyncio
async def test_verification_not_inherited_across_versions(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]
    v1_id = tool["current_version_id"]

    verified = await db_client.post(
        f"{API_TOOLS}/{tool_id}/versions/{v1_id}/verifications",
        json={
            "status": "VERIFIED",
            "criteria_version": "tool-verification-v1",
            "test_execution_id": str(uuid.uuid4()),
            "evidence_blob_id": str(uuid.uuid4()),
            "result_summary": {
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
            },
        },
    )
    assert verified.status_code == 201, verified.text
    v1_verification_id = verified.json()["id"]

    # Force schema change via tools_override (independent of SCHEMA_CHANGE call counting).
    changed = TestMCPScenario(TestMCPScenario.HEALTHY)
    changed.tools_override = [
        {
            "name": "echo",
            "description": "Echo input back (v2)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "locale": {"type": "string"},
                },
                "required": ["message"],
            },
        },
        {
            "name": "lookup_weather",
            "description": "Lookup weather for a city",
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    ]
    override_mcp_client(changed.build_http_client())
    discovery = await db_client.post(
        f"{API_SERVERS}/{server['id']}/discoveries",
        json={"apply_changes": True},
    )
    assert discovery.status_code == 201
    assert discovery.json()["success"] is True

    refreshed = await db_client.get(f"{API_TOOLS}/{tool_id}")
    v2_id = refreshed.json()["current_version_id"]
    assert v2_id != v1_id

    v2_list = await db_client.get(f"{API_TOOLS}/{tool_id}/versions/{v2_id}/verifications")
    assert v2_list.status_code == 200
    assert v2_list.json()["total"] == 0
    assert v2_list.json()["items"] == []

    v1_still = await db_client.get(
        f"{API_TOOLS}/{tool_id}/versions/{v1_id}/verifications/{v1_verification_id}"
    )
    assert v1_still.status_code == 200
    assert v1_still.json()["status"] == "VERIFIED"


@pytest.mark.asyncio
async def test_policy_persists_across_tool_version_change(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    server, tool = await _discover_tools(db_client, override_mcp_client)
    tool_id = tool["id"]

    created = await db_client.put(
        f"{API_TOOLS}/{tool_id}/policy",
        json={
            "risk_class": "IDEMPOTENT_WRITE",
            "timeout_ms": 5000,
            "max_attempts": 3,
            "max_result_bytes": 4096,
            "allow_auto_select": True,
        },
    )
    assert created.status_code == 200
    policy_id = created.json()["id"]

    changed = TestMCPScenario(TestMCPScenario.HEALTHY)
    changed.tools_override = [
        {
            "name": "echo",
            "description": "Echo input back (v2)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "locale": {"type": "string"},
                },
                "required": ["message"],
            },
        },
        {
            "name": "lookup_weather",
            "description": "Lookup weather for a city",
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    ]
    override_mcp_client(changed.build_http_client())
    discovery = await db_client.post(
        f"{API_SERVERS}/{server['id']}/discoveries",
        json={"apply_changes": True},
    )
    assert discovery.status_code == 201

    policy = await db_client.get(f"{API_TOOLS}/{tool_id}/policy")
    assert policy.status_code == 200
    assert policy.json()["id"] == policy_id
    assert policy.json()["risk_class"] == "IDEMPOTENT_WRITE"


@pytest.mark.asyncio
async def test_verification_extra_field_and_invalid_status_422(
    db_client: AsyncClient,
    override_mcp_client,
) -> None:
    _server, tool = await _discover_tools(db_client, override_mcp_client)
    version_id = tool["current_version_id"]

    extra = await db_client.post(
        f"{API_TOOLS}/{tool['id']}/versions/{version_id}/verifications",
        json={
            "status": "PENDING",
            "criteria_version": "v1",
            "verified_by": str(uuid.uuid4()),
        },
    )
    assert extra.status_code == 422

    bad_status = await db_client.post(
        f"{API_TOOLS}/{tool['id']}/versions/{version_id}/verifications",
        json={"status": "PASSED", "criteria_version": "v1"},
    )
    assert bad_status.status_code == 422
