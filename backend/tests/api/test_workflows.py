"""SQLite-backed API tests for Workflow registry foundation."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1/workflows"


@pytest.fixture
async def db_client(authenticated_db_client):
    """Protected API tests use a real Session + CSRF (no auth bypass)."""
    return authenticated_db_client


async def _seed_tool_version(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"wf-api-{uuid.uuid4().hex[:8]}",
            name="Workflow API Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool_{uuid.uuid4().hex[:6]}",
            display_name="api_tool",
            tags=[],
            status="ACTIVE",
        )
        version = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="api",
            input_schema={
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        )
        await session.commit()
        return version.id


async def _seed_approval_policy(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str = "ACTIVE",
) -> uuid.UUID:
    from app.repositories.approval_policy import ApprovalPolicyRepository

    async with db_session_factory() as session:
        policy = await ApprovalPolicyRepository(session).create(
            code=f"ap-api-{uuid.uuid4().hex[:8]}",
            name="API Approval",
            status=status,
        )
        await session.commit()
        return policy.id


def _tool_plan(tool_version_id: uuid.UUID) -> dict[str, Any]:
    from app.domain.enums import AuthorableStepType, BindingKind
    from app.schemas.execution_plan import (
        EXECUTION_PLAN_SCHEMA_VERSION,
        default_plan_limits,
    )

    agent_version_id = str(uuid.uuid4())
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "api workflow fixture",
        "source": {"type": "AGENT", "agent_version_id": agent_version_id},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": [
            {
                "id": "step_a",
                "name": "step_a",
                "type": AuthorableStepType.TOOL.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "tool_version_id": str(tool_version_id),
                    "bindings": {
                        "location": {
                            "kind": BindingKind.LITERAL.value,
                            "value": "Seoul",
                        }
                    },
                },
            }
        ],
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": ["step_a"],
        },
    }


async def _create_workflow(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {"name": "Ops Workflow", "description": "ops", "visibility": "PRIVATE"}
    body.update(overrides)
    response = await client.post(API, json=body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_workflow_crud_list_and_patch(db_client: AsyncClient) -> None:
    created = await _create_workflow(db_client, name="Alpha Workflow")
    assert created["status"] == "DRAFT"
    assert created["visibility"] == "PRIVATE"
    assert created["lock_version"] == 1
    assert created["current_version_id"] is None
    assert "code" in created

    detail = await db_client.get(f"{API}/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Alpha Workflow"

    listing = await db_client.get(API, params={"q": "Alpha"})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    patched = await db_client.patch(
        f"{API}/{created['id']}",
        headers={"If-Match": "1"},
        json={"name": "Alpha Renamed", "visibility": "INTERNAL", "lock_version": 1},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "Alpha Renamed"
    assert body["visibility"] == "INTERNAL"
    assert body["lock_version"] == 2


@pytest.mark.asyncio
async def test_workflow_create_extra_forbid_and_if_match(
    db_client: AsyncClient,
) -> None:
    forbidden = await db_client.post(
        API,
        json={
            "name": "Bad",
            "status": "ACTIVE",
            "code": "hijack",
        },
    )
    assert forbidden.status_code == 422

    created = await _create_workflow(db_client)
    workflow_id = created["id"]

    missing_if_match = await db_client.patch(
        f"{API}/{workflow_id}",
        json={"name": "nope"},
    )
    assert missing_if_match.status_code == 422

    stale = await db_client.patch(
        f"{API}/{workflow_id}",
        headers={"If-Match": "99"},
        json={"name": "nope", "lock_version": 99},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    missing = await db_client.get(f"{API}/{uuid.uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_versions_create_list_get_and_cross_workflow_404(
    db_client: AsyncClient,
    db_session_factory,
) -> None:
    tv = await _seed_tool_version(db_session_factory)
    workflow = await _create_workflow(db_client)
    workflow_id = workflow["id"]
    plan = _tool_plan(tv)

    v1 = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": plan, "change_summary": "v1"},
    )
    assert v1.status_code == 201, v1.text
    assert v1.json()["version_no"] == 1
    assert v1.json()["status"] == "DRAFT"
    assert v1.json()["validation_status"] == "INVALID"
    v1_id = v1.json()["id"]

    v2 = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": plan, "change_summary": "v2"},
    )
    assert v2.status_code == 201
    assert v2.json()["version_no"] == 2

    listing = await db_client.get(f"{API}/{workflow_id}/versions")
    assert listing.status_code == 200
    assert listing.json()["total"] == 2
    assert listing.json()["items"][0]["version_no"] == 2

    detail = await db_client.get(f"{API}/{workflow_id}/versions/{v1_id}")
    assert detail.status_code == 200

    other = await _create_workflow(db_client, name="Other")
    wrong = await db_client.get(f"{API}/{other['id']}/versions/{v1_id}")
    assert wrong.status_code == 404

    injected = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={
            "plan_definition": plan,
            "status": "PUBLISHED",
            "version_no": 99,
            "validation_status": "VALID",
        },
    )
    assert injected.status_code == 422


@pytest.mark.asyncio
async def test_put_plan_validate_publish_deprecate(
    db_client: AsyncClient,
    db_session_factory,
) -> None:
    tv = await _seed_tool_version(db_session_factory)
    workflow = await _create_workflow(db_client)
    workflow_id = workflow["id"]
    plan = _tool_plan(tv)

    created = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": {}, "change_summary": "empty"},
    )
    assert created.status_code == 201
    version_id = created.json()["id"]

    put = await db_client.put(
        f"{API}/{workflow_id}/versions/{version_id}/plan",
        json={"plan_definition": plan, "change_summary": "real plan"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["validation_status"] == "INVALID"
    assert put.json()["validation_report"] is None
    assert put.json()["plan_definition"]["steps"][0]["id"] == "step_a"

    non_object = await db_client.put(
        f"{API}/{workflow_id}/versions/{version_id}/plan",
        json={"plan_definition": ["nope"]},
    )
    assert non_object.status_code == 422

    unvalidated = await db_client.post(
        f"{API}/{workflow_id}/versions/{version_id}/publish"
    )
    assert unvalidated.status_code == 409

    validated = await db_client.post(
        f"{API}/{workflow_id}/versions/{version_id}/validate"
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["validation_status"] == "VALID"

    published = await db_client.post(
        f"{API}/{workflow_id}/versions/{version_id}/publish"
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED"

    agent = await db_client.get(f"{API}/{workflow_id}")
    assert agent.json()["current_version_id"] == version_id
    assert agent.json()["status"] == "DRAFT"

    immutable = await db_client.put(
        f"{API}/{workflow_id}/versions/{version_id}/plan",
        json={"plan_definition": plan},
    )
    assert immutable.status_code == 409

    current_dep = await db_client.post(
        f"{API}/{workflow_id}/versions/{version_id}/deprecate"
    )
    assert current_dep.status_code == 409

    # Clone + publish v2 → v1 deprecated via publish
    clone = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"source_version_id": version_id, "change_summary": "v2"},
    )
    assert clone.status_code == 201
    v2_id = clone.json()["id"]
    await db_client.post(f"{API}/{workflow_id}/versions/{v2_id}/validate")
    pub2 = await db_client.post(f"{API}/{workflow_id}/versions/{v2_id}/publish")
    assert pub2.status_code == 200

    old = await db_client.get(f"{API}/{workflow_id}/versions/{version_id}")
    assert old.json()["status"] == "DEPRECATED"

    idem = await db_client.post(
        f"{API}/{workflow_id}/versions/{version_id}/deprecate"
    )
    assert idem.status_code == 200
    assert idem.json()["status"] == "DEPRECATED"

    draft = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": plan},
    )
    draft_dep = await db_client.post(
        f"{API}/{workflow_id}/versions/{draft.json()['id']}/deprecate"
    )
    assert draft_dep.status_code == 409


@pytest.mark.asyncio
async def test_active_requires_published_and_archived_immutable(
    db_client: AsyncClient,
    db_session_factory,
) -> None:
    tv = await _seed_tool_version(db_session_factory)
    created = await _create_workflow(db_client)
    workflow_id = created["id"]

    active_reject = await db_client.patch(
        f"{API}/{workflow_id}",
        headers={"If-Match": "1"},
        json={"status": "ACTIVE", "lock_version": 1},
    )
    assert active_reject.status_code == 409

    version = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": _tool_plan(tv)},
    )
    version_id = version.json()["id"]
    await db_client.post(f"{API}/{workflow_id}/versions/{version_id}/validate")
    await db_client.post(f"{API}/{workflow_id}/versions/{version_id}/publish")

    agent = await db_client.get(f"{API}/{workflow_id}")
    lock = agent.json()["lock_version"]
    active_ok = await db_client.patch(
        f"{API}/{workflow_id}",
        headers={"If-Match": str(lock)},
        json={"status": "ACTIVE", "lock_version": lock},
    )
    assert active_ok.status_code == 200
    assert active_ok.json()["status"] == "ACTIVE"

    lock2 = active_ok.json()["lock_version"]
    archived = await db_client.patch(
        f"{API}/{workflow_id}",
        headers={"If-Match": str(lock2)},
        json={"status": "ARCHIVED", "lock_version": lock2},
    )
    assert archived.status_code == 200
    lock3 = archived.json()["lock_version"]
    revive = await db_client.patch(
        f"{API}/{workflow_id}",
        headers={"If-Match": str(lock3)},
        json={"status": "ACTIVE", "lock_version": lock3},
    )
    assert revive.status_code == 409


@pytest.mark.asyncio
async def test_validate_invalid_plan_and_approval_policy(
    db_client: AsyncClient,
    db_session_factory,
) -> None:
    tv = await _seed_tool_version(db_session_factory)
    await _seed_approval_policy(db_session_factory, status="ACTIVE")
    workflow = await _create_workflow(db_client)
    workflow_id = workflow["id"]

    created = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": {}},
    )
    version_id = created.json()["id"]
    validated = await db_client.post(
        f"{API}/{workflow_id}/versions/{version_id}/validate"
    )
    assert validated.status_code == 200
    assert validated.json()["validation_status"] == "INVALID"
    assert validated.json()["validation_report"]["valid"] is False

    # Missing tool version
    missing_tv = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": _tool_plan(uuid.uuid4())},
    )
    mid = missing_tv.json()["id"]
    bad = await db_client.post(f"{API}/{workflow_id}/versions/{mid}/validate")
    assert bad.status_code == 200
    codes = [e["code"] for e in bad.json()["validation_report"]["errors"]]
    assert "WORKFLOW_TOOL_VERSION_NOT_FOUND" in codes

    # Valid tool plan
    good = await db_client.post(
        f"{API}/{workflow_id}/versions",
        json={"plan_definition": _tool_plan(tv)},
    )
    gid = good.json()["id"]
    ok = await db_client.post(f"{API}/{workflow_id}/versions/{gid}/validate")
    assert ok.status_code == 200
    assert ok.json()["validation_status"] == "VALID"
