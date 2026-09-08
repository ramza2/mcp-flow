"""SQLite-backed API tests for Agent registry foundation."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

API = "/api/v1/agents"
PROFILES_API = "/api/v1/model-profiles/llm"


@pytest.fixture
async def db_client(authenticated_db_client):
    """Protected API tests use a real Session + CSRF (no auth bypass)."""
    return authenticated_db_client


def _selection(**overrides: Any) -> dict[str, Any]:
    payload = {
        "auto_select_threshold": 0.82,
        "confirmation_threshold": 0.60,
        "max_candidates": 5,
    }
    payload.update(overrides)
    return payload


def _version_body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "system_instruction": "허용된 Tool만 사용하여 안전하게 업무를 계획한다.",
        "llm_profile_id": str(uuid.uuid4()),
        "request_schema_version": "1.0",
        "plan_schema_version": "1.0",
        "selection_settings": _selection(),
        "planning_settings": {},
        "response_settings": {},
        "change_summary": "initial",
    }
    payload.update(overrides)
    return payload


async def _create_agent(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {"name": "Ops Agent", "description": "ops", "visibility": "PRIVATE"}
    body.update(overrides)
    response = await client.post(API, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_llm_profile(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "name": "Test LLM",
        "provider": "OPENAI_COMPATIBLE",
        "model": "gpt-test",
        "base_url": "https://llm.test/v1",
        "parameters": {"temperature": 0.2},
    }
    body.update(overrides)
    response = await client.post(PROFILES_API, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_tool(db_session_factory, *, status: str = "ACTIVE") -> uuid.UUID:
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    async with db_session_factory() as session:
        server = await MCPServerRepository(session).create(
            code=f"agt-{uuid.uuid4().hex[:8]}",
            name="Agent Tool Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
        )
        tool = await MCPToolRepository(session).create_tool(
            mcp_server_id=server.id,
            remote_name=f"tool_{uuid.uuid4().hex[:6]}",
            status=status,
        )
        await session.commit()
        return tool.id


@pytest.mark.asyncio
async def test_agent_crud_list_and_patch(db_client: AsyncClient) -> None:
    created = await _create_agent(db_client, name="Alpha Agent")
    assert created["status"] == "DRAFT"
    assert created["visibility"] == "PRIVATE"
    assert created["lock_version"] == 1
    assert created["current_version_id"] is None
    assert "code" in created

    detail = await db_client.get(f"{API}/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Alpha Agent"

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
async def test_agent_patch_rejects_unknown_and_forbidden_fields(
    db_client: AsyncClient,
) -> None:
    created = await _create_agent(db_client)
    agent_id = created["id"]

    unknown = await db_client.patch(
        f"{API}/{agent_id}",
        headers={"If-Match": "1"},
        json={"lock_version": 1, "current_version_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 422

    code_patch = await db_client.patch(
        f"{API}/{agent_id}",
        headers={"If-Match": "1"},
        json={"lock_version": 1, "code": "hijacked"},
    )
    assert code_patch.status_code == 422

    stale = await db_client.patch(
        f"{API}/{agent_id}",
        headers={"If-Match": "99"},
        json={"name": "nope", "lock_version": 99},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    missing = await db_client.get(f"{API}/{uuid.uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_agent_active_and_archived_rules(
    db_client: AsyncClient, db_session_factory
) -> None:
    created = await _create_agent(db_client)
    agent_id = created["id"]
    profile = await _create_llm_profile(db_client)

    active_reject = await db_client.patch(
        f"{API}/{agent_id}",
        headers={"If-Match": "1"},
        json={"status": "ACTIVE", "lock_version": 1},
    )
    assert active_reject.status_code == 409

    version = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile["id"]),
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]

    validated = await db_client.post(f"{API}/{agent_id}/versions/{version_id}/validate")
    assert validated.status_code == 200
    assert validated.json()["validation_status"] == "VALID"

    published = await db_client.post(f"{API}/{agent_id}/versions/{version_id}/publish")
    assert published.status_code == 200, published.text

    agent = await db_client.get(f"{API}/{agent_id}")
    lock = agent.json()["lock_version"]
    active_ok = await db_client.patch(
        f"{API}/{agent_id}",
        headers={"If-Match": str(lock)},
        json={"status": "ACTIVE", "lock_version": lock},
    )
    assert active_ok.status_code == 200
    assert active_ok.json()["status"] == "ACTIVE"

    lock2 = active_ok.json()["lock_version"]
    archived = await db_client.patch(
        f"{API}/{agent_id}",
        headers={"If-Match": str(lock2)},
        json={"status": "ARCHIVED", "lock_version": lock2},
    )
    assert archived.status_code == 200
    lock3 = archived.json()["lock_version"]
    revive = await db_client.patch(
        f"{API}/{agent_id}",
        headers={"If-Match": str(lock3)},
        json={"status": "ACTIVE", "lock_version": lock3},
    )
    assert revive.status_code == 409


@pytest.mark.asyncio
async def test_version_create_list_detail_and_injection_rejected(
    db_client: AsyncClient,
) -> None:
    agent = await _create_agent(db_client)
    agent_id = agent["id"]

    v1 = await db_client.post(f"{API}/{agent_id}/versions", json=_version_body())
    assert v1.status_code == 201
    assert v1.json()["version_no"] == 1
    assert v1.json()["status"] == "DRAFT"
    assert v1.json()["validation_status"] == "INVALID"

    v2 = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(change_summary="second"),
    )
    assert v2.status_code == 201
    assert v2.json()["version_no"] == 2

    listing = await db_client.get(f"{API}/{agent_id}/versions")
    assert listing.status_code == 200
    assert listing.json()["total"] == 2
    assert listing.json()["items"][0]["version_no"] == 2

    detail = await db_client.get(f"{API}/{agent_id}/versions/{v1.json()['id']}")
    assert detail.status_code == 200

    other = await _create_agent(db_client, name="Other")
    wrong = await db_client.get(
        f"{API}/{other['id']}/versions/{v1.json()['id']}"
    )
    assert wrong.status_code == 404

    injected = await db_client.post(
        f"{API}/{agent_id}/versions",
        json={
            **_version_body(),
            "status": "PUBLISHED",
            "version_no": 99,
            "validation_status": "VALID",
        },
    )
    assert injected.status_code == 422


@pytest.mark.asyncio
async def test_tool_grants_replace_and_draft_only(
    db_client: AsyncClient, db_session_factory
) -> None:
    tool_a = await _seed_tool(db_session_factory, status="DISCOVERED")
    tool_b = await _seed_tool(db_session_factory, status="MISSING")
    tool_c = await _seed_tool(db_session_factory, status="BLOCKED")

    agent = await _create_agent(db_client)
    agent_id = agent["id"]
    profile = await _create_llm_profile(db_client)
    version = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile["id"]),
    )
    version_id = version.json()["id"]

    put = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json={
            "items": [
                {
                    "mcp_tool_id": str(tool_a),
                    "effect": "ALLOW",
                    "parameter_constraints": {},
                    "requires_confirmation": False,
                },
                {
                    "mcp_tool_id": str(tool_b),
                    "effect": "DENY",
                    "requires_confirmation": True,
                },
                {
                    "mcp_tool_id": str(tool_c),
                    "effect": "ALLOW",
                },
            ]
        },
    )
    assert put.status_code == 200, put.text
    assert len(put.json()["items"]) == 3

    dup = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json={
            "items": [
                {"mcp_tool_id": str(tool_a), "effect": "ALLOW"},
                {"mcp_tool_id": str(tool_a), "effect": "DENY"},
            ]
        },
    )
    assert dup.status_code == 422

    missing_tool = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json={"items": [{"mcp_tool_id": str(uuid.uuid4()), "effect": "ALLOW"}]},
    )
    assert missing_tool.status_code == 422

    # replace with single grant
    put2 = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json={"items": [{"mcp_tool_id": str(tool_a), "effect": "ALLOW"}]},
    )
    assert put2.status_code == 200
    assert len(put2.json()["items"]) == 1

    await db_client.post(f"{API}/{agent_id}/versions/{version_id}/validate")
    await db_client.post(f"{API}/{agent_id}/versions/{version_id}/publish")

    published_put = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json={"items": []},
    )
    assert published_put.status_code == 409


@pytest.mark.asyncio
async def test_validation_rules(db_client: AsyncClient) -> None:
    agent = await _create_agent(db_client)
    agent_id = agent["id"]
    profile = await _create_llm_profile(db_client)
    profile_id = profile["id"]

    blank = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile_id, system_instruction="   "),
    )
    # Field min_length=1 may reject at schema; blank spaces pass min_length then INVALID
    if blank.status_code == 201:
        result = await db_client.post(
            f"{API}/{agent_id}/versions/{blank.json()['id']}/validate"
        )
        assert result.status_code == 200
        assert result.json()["validation_status"] == "INVALID"

    bad_schema = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile_id, request_schema_version="2.0"),
    )
    assert bad_schema.status_code == 201
    result = await db_client.post(
        f"{API}/{agent_id}/versions/{bad_schema.json()['id']}/validate"
    )
    assert result.json()["validation_status"] == "INVALID"

    bad_plan = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile_id, plan_schema_version="9.9"),
    )
    result = await db_client.post(
        f"{API}/{agent_id}/versions/{bad_plan.json()['id']}/validate"
    )
    assert result.json()["validation_status"] == "INVALID"

    bad_threshold = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(
            llm_profile_id=profile_id,
            selection_settings=_selection(
                auto_select_threshold=0.2, confirmation_threshold=0.8
            ),
        ),
    )
    assert bad_threshold.status_code == 422

    missing_profile = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=str(uuid.uuid4())),
    )
    missing_validated = await db_client.post(
        f"{API}/{agent_id}/versions/{missing_profile.json()['id']}/validate"
    )
    assert missing_validated.status_code == 200
    assert missing_validated.json()["validation_status"] == "INVALID"
    missing_report = missing_validated.json()["validation_report"]
    assert missing_report["dependency_checks"]["llm_profile"] == "NOT_FOUND"
    assert any(err["code"] == "LLM_PROFILE_NOT_FOUND" for err in missing_report["errors"])

    ok = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile_id),
    )
    validated = await db_client.post(
        f"{API}/{agent_id}/versions/{ok.json()['id']}/validate"
    )
    assert validated.status_code == 200
    assert validated.json()["validation_status"] == "VALID"
    report = validated.json()["validation_report"]
    assert report["valid"] is True
    assert report["dependency_checks"]["llm_profile"] == "OK"


@pytest.mark.asyncio
async def test_publish_blocks_when_llm_profile_missing_after_valid(
    db_client: AsyncClient, db_session_factory
) -> None:
    """Pre-existing VALID draft with deleted/missing profile must not publish."""

    from app.domain.enums import AgentVersionValidationStatus
    from app.repositories.agent_version import AgentVersionRepository

    agent = await _create_agent(db_client)
    agent_id = agent["id"]
    missing_profile_id = uuid.uuid4()
    version = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=str(missing_profile_id)),
    )
    version_id = uuid.UUID(version.json()["id"])

    async with db_session_factory() as session:
        row = await AgentVersionRepository(session).get_for_agent(
            uuid.UUID(agent_id), version_id
        )
        assert row is not None
        await AgentVersionRepository(session).set_validation(
            row,
            validation_status=str(AgentVersionValidationStatus.VALID),
            validation_report={"schema_version": "1.0", "valid": True, "errors": []},
        )
        await session.commit()

    blocked = await db_client.post(f"{API}/{agent_id}/versions/{version_id}/publish")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "RESOURCE_CONFLICT"

    profile = await _create_llm_profile(db_client)
    version_ok = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile["id"]),
    )
    await db_client.post(
        f"{API}/{agent_id}/versions/{version_ok.json()['id']}/validate"
    )
    published = await db_client.post(
        f"{API}/{agent_id}/versions/{version_ok.json()['id']}/publish"
    )
    assert published.status_code == 200


@pytest.mark.asyncio
async def test_publish_deprecate_and_clone(db_client: AsyncClient, db_session_factory) -> None:
    tool_id = await _seed_tool(db_session_factory)
    agent = await _create_agent(db_client)
    agent_id = agent["id"]
    profile = await _create_llm_profile(db_client)

    v1 = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile["id"]),
    )
    v1_id = v1.json()["id"]
    await db_client.put(
        f"{API}/{agent_id}/versions/{v1_id}/tool-grants",
        json={"items": [{"mcp_tool_id": str(tool_id), "effect": "ALLOW"}]},
    )

    unvalidated_publish = await db_client.post(
        f"{API}/{agent_id}/versions/{v1_id}/publish"
    )
    assert unvalidated_publish.status_code == 409

    await db_client.post(f"{API}/{agent_id}/versions/{v1_id}/validate")
    published = await db_client.post(f"{API}/{agent_id}/versions/{v1_id}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "PUBLISHED"
    assert published.json()["published_at"] is not None

    agent_after = await db_client.get(f"{API}/{agent_id}")
    assert agent_after.json()["current_version_id"] == v1_id

    # current deprecate blocked
    current_dep = await db_client.post(f"{API}/{agent_id}/versions/{v1_id}/deprecate")
    assert current_dep.status_code == 409

    # clone from source
    clone = await db_client.post(
        f"{API}/{agent_id}/versions",
        json={"source_version_id": v1_id, "change_summary": "from v1"},
    )
    assert clone.status_code == 201, clone.text
    v2 = clone.json()
    assert v2["id"] != v1_id
    assert v2["version_no"] == 2
    assert v2["status"] == "DRAFT"
    assert v2["system_instruction"] == v1.json()["system_instruction"]
    assert v2["content_hash"] == v1.json()["content_hash"]
    assert v2["published_at"] is None
    assert v2["deprecated_at"] is None

    grants = await db_client.get(f"{API}/{agent_id}/versions/{v2['id']}/tool-grants")
    assert len(grants.json()["items"]) == 1

    source_after = await db_client.get(f"{API}/{agent_id}/versions/{v1_id}")
    assert source_after.json()["status"] == "PUBLISHED"

    await db_client.post(f"{API}/{agent_id}/versions/{v2['id']}/validate")
    pub2 = await db_client.post(f"{API}/{agent_id}/versions/{v2['id']}/publish")
    assert pub2.status_code == 200
    assert pub2.json()["status"] == "PUBLISHED"

    old = await db_client.get(f"{API}/{agent_id}/versions/{v1_id}")
    assert old.json()["status"] == "DEPRECATED"

    agent2 = await db_client.get(f"{API}/{agent_id}")
    assert agent2.json()["current_version_id"] == v2["id"]

    # create v3 and publish (v2 becomes deprecated via publish)
    v3 = await db_client.post(
        f"{API}/{agent_id}/versions",
        json={"source_version_id": v2["id"]},
    )
    await db_client.post(f"{API}/{agent_id}/versions/{v3.json()['id']}/validate")
    await db_client.post(f"{API}/{agent_id}/versions/{v3.json()['id']}/publish")

    # v2 is now DEPRECATED via publish; create another published non-current path:
    # deprecate non-current PUBLISHED: make v4 current, leaving... only one published at a time.
    # So non-current PUBLISHED shouldn't exist after our publish policy.
    # Test DRAFT deprecate conflict and DEPRECATED idempotent on v2.
    draft_dep = await db_client.post(
        f"{API}/{agent_id}/versions/{v3.json()['id']}/deprecate"
    )
    # v3 is current — conflict
    assert draft_dep.status_code == 409

    idem = await db_client.post(f"{API}/{agent_id}/versions/{v2['id']}/deprecate")
    assert idem.status_code == 200
    assert idem.json()["status"] == "DEPRECATED"

    draft = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile["id"]),
    )
    draft_dep2 = await db_client.post(
        f"{API}/{agent_id}/versions/{draft.json()['id']}/deprecate"
    )
    assert draft_dep2.status_code == 409


@pytest.mark.asyncio
async def test_grant_change_invalidates_validation_and_blocks_publish(
    db_client: AsyncClient, db_session_factory
) -> None:
    tool_a = await _seed_tool(db_session_factory, status="ACTIVE")
    tool_b = await _seed_tool(db_session_factory, status="INACTIVE")
    agent = await _create_agent(db_client)
    agent_id = agent["id"]
    profile = await _create_llm_profile(db_client)
    version = await db_client.post(
        f"{API}/{agent_id}/versions",
        json=_version_body(llm_profile_id=profile["id"]),
    )
    version_id = version.json()["id"]

    grant_a = {
        "items": [
            {
                "mcp_tool_id": str(tool_a),
                "effect": "ALLOW",
                "parameter_constraints": {"max": 1},
                "requires_confirmation": False,
            }
        ]
    }
    put_a = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json=grant_a,
    )
    assert put_a.status_code == 200

    validated = await db_client.post(
        f"{API}/{agent_id}/versions/{version_id}/validate"
    )
    assert validated.status_code == 200
    assert validated.json()["validation_status"] == "VALID"
    assert validated.json()["validation_report"]["valid"] is True

    # identical PUT preserves VALID
    same = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json=grant_a,
    )
    assert same.status_code == 200
    detail_same = await db_client.get(f"{API}/{agent_id}/versions/{version_id}")
    assert detail_same.json()["validation_status"] == "VALID"
    assert detail_same.json()["validation_report"] is not None

    # attribute change (ALLOW → DENY) invalidates
    attr_change = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json={
            "items": [
                {
                    "mcp_tool_id": str(tool_a),
                    "effect": "DENY",
                    "parameter_constraints": {"max": 1},
                    "requires_confirmation": False,
                }
            ]
        },
    )
    assert attr_change.status_code == 200
    detail_attr = await db_client.get(f"{API}/{agent_id}/versions/{version_id}")
    assert detail_attr.json()["validation_status"] == "INVALID"
    assert detail_attr.json()["validation_report"] is None

    await db_client.post(f"{API}/{agent_id}/versions/{version_id}/validate")
    assert (
        await db_client.get(f"{API}/{agent_id}/versions/{version_id}")
    ).json()["validation_status"] == "VALID"

    # membership change invalidates and blocks publish until revalidate
    changed = await db_client.put(
        f"{API}/{agent_id}/versions/{version_id}/tool-grants",
        json={
            "items": [
                {
                    "mcp_tool_id": str(tool_b),
                    "effect": "ALLOW",
                    "parameter_constraints": {},
                    "requires_confirmation": True,
                }
            ]
        },
    )
    assert changed.status_code == 200
    detail = await db_client.get(f"{API}/{agent_id}/versions/{version_id}")
    assert detail.json()["validation_status"] == "INVALID"
    assert detail.json()["validation_report"] is None

    blocked = await db_client.post(f"{API}/{agent_id}/versions/{version_id}/publish")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "RESOURCE_CONFLICT"

    revalidated = await db_client.post(
        f"{API}/{agent_id}/versions/{version_id}/validate"
    )
    assert revalidated.status_code == 200
    assert revalidated.json()["validation_status"] == "VALID"

    published = await db_client.post(f"{API}/{agent_id}/versions/{version_id}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "PUBLISHED"
