"""API tests for GET /executions history (own vs execution.read)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.domain.enums import ExecutionStatus, ExecutionTriggerType
from app.repositories.user import UserRepository
from tests.helpers.execution_ops import seed_execution, seed_user

API = "/api/v1/executions"
PASSWORD = "correct-horse-battery-staple"


async def _client_for(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    with_execution_read: bool = False,
) -> tuple[AsyncClient, uuid.UUID]:
    client = unauthenticated_db_client
    async with db_session_factory() as session:
        user_id = await seed_user(session, with_execution_read=with_execution_read)
        user = await UserRepository(session).get(user_id)
        assert user is not None
        await UserRepository(session).set_password_hash(user.id, hash_password(PASSWORD))
        await session.commit()
        username = user.username

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return client, user_id


@pytest.mark.asyncio
async def test_own_history_vs_global_execution_read(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Two users without execution.read
    client_a, user_a = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=False
    )
    async with db_session_factory() as session:
        user_b = await seed_user(session, with_execution_read=False)
        exec_a = await seed_execution(session, requester_id=user_a)
        exec_b = await seed_execution(session, requester_id=user_b)
        await session.commit()
        exec_a_id, exec_b_id = exec_a.id, exec_b.id

    listed = await client_a.get(API)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    ids = {i["id"] for i in body["items"]}
    assert str(exec_a_id) in ids
    assert str(exec_b_id) not in ids
    assert body["total"] == len(body["items"]) or body["total"] >= 1

    detail_b = await client_a.get(f"{API}/{exec_b_id}")
    assert detail_b.status_code == 404
    steps_b = await client_a.get(f"{API}/{exec_b_id}/steps")
    assert steps_b.status_code == 404

    # Foreign requester_id filter → 403
    forbidden = await client_a.get(API, params={"requester_id": str(user_b)})
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "AUTH_FORBIDDEN"

    # Self requester_id ok
    self_ok = await client_a.get(API, params={"requester_id": str(user_a)})
    assert self_ok.status_code == 200

    # Grant execution.read to A
    from app.repositories.role import PermissionRepository
    from app.schemas.auth import (
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.role import RoleService
    from app.services.user import UserService

    async with db_session_factory() as session:
        perm = await PermissionRepository(session).get_by_code("execution.read")
        assert perm is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"er-{uuid.uuid4().hex[:6]}", name="ER")
        )
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[perm.id]),
            expected_lock_version=1,
        )
        user = await UserRepository(session).get(user_a)
        assert user is not None
        roles = await UserService(session).list_roles(user_a)
        await UserService(session).replace_roles(
            user_a,
            UserRoleReplaceRequest(role_ids=[r.id for r in roles] + [role.id]),
            expected_lock_version=int(user.lock_version),
        )
        await session.commit()

    # Re-login to refresh? Permissions are DB-checked per request — no need.
    listed2 = await client_a.get(API)
    assert listed2.status_code == 200
    ids2 = {i["id"] for i in listed2.json()["items"]}
    assert str(exec_a_id) in ids2
    assert str(exec_b_id) in ids2

    detail_b2 = await client_a.get(f"{API}/{exec_b_id}")
    assert detail_b2.status_code == 200
    d = detail_b2.json()
    for forbidden_key in (
        "plan_snapshot",
        "input_snapshot",
        "policy_snapshot",
        "error_message",
        "cancel_reason",
        "worker_id",
        "lease_token",
        "lease_expires_at",
    ):
        assert forbidden_key not in d


@pytest.mark.asyncio
async def test_list_filters_sort_and_validation(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, user_id = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=True
    )
    base = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    async with db_session_factory() as session:
        e1 = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.FAILED.value,
            error_code="TOOL_FAILED",
            requested_at=base,
            started_at=base,
            finished_at=base + timedelta(seconds=10),
            trace_id="trace-abc",
        )
        e2 = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.TIMED_OUT.value,
            error_code="STEP_TIMEOUT_EXCEEDED",
            requested_at=base + timedelta(seconds=1),
            started_at=base + timedelta(seconds=1),
            finished_at=base + timedelta(seconds=20),
        )
        e3 = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.SUCCEEDED.value,
            requested_at=base + timedelta(seconds=2),
            started_at=base + timedelta(seconds=2),
            finished_at=base + timedelta(seconds=5),
        )
        await session.commit()

    multi = await client.get(
        API, params={"status": "FAILED,TIMED_OUT", "page_size": 50}
    )
    assert multi.status_code == 200
    statuses = {i["status"] for i in multi.json()["items"] if i["id"] in {str(e1.id), str(e2.id), str(e3.id)}}
    assert statuses == {"FAILED", "TIMED_OUT"}

    bad_status = await client.get(API, params={"status": "FAILED,NOPE"})
    assert bad_status.status_code == 422

    bad_sort = await client.get(API, params={"sort": "plan_snapshot"})
    assert bad_sort.status_code == 422

    naive = await client.get(API, params={"from": "2026-10-07T12:00:00"})
    assert naive.status_code == 422

    window = await client.get(
        API,
        params={
            "from": base.isoformat().replace("+00:00", "Z"),
            "to": (base + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        },
    )
    assert window.status_code == 200
    # inclusive from, exclusive to → e1,e2
    win_ids = {i["id"] for i in window.json()["items"]}
    assert str(e1.id) in win_ids
    assert str(e2.id) in win_ids
    assert str(e3.id) not in win_ids

    q = await client.get(API, params={"q": "trace-abc"})
    assert q.status_code == 200
    assert any(i["id"] == str(e1.id) for i in q.json()["items"])

    err = await client.get(API, params={"error_code": "TOOL_FAILED"})
    assert err.status_code == 200
    assert all(
        i["error_code"] == "TOOL_FAILED"
        for i in err.json()["items"]
        if i["id"] == str(e1.id)
    )

    # List omits secret fields
    item = next(i for i in multi.json()["items"] if i["id"] == str(e1.id))
    assert item["error_category"] == "tool"
    assert "plan_snapshot" not in item
    assert "result_summary" not in item


@pytest.mark.asyncio
async def test_steps_and_attempts_safe_projection(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import StepAttemptStatus, StepStatus, ToolCallNormalizedStatus
    from app.repositories.execution import ExecutionRepository
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    client, user_id = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=False
    )
    async with db_session_factory() as session:
        execution = await seed_execution(session, requester_id=user_id)
        repo = ExecutionRepository(session)
        parent = await repo.create_step(
            execution_id=execution.id,
            step_key="loop",
            step_type="LOOP",
            mcp_tool_version_id=None,
            parent_step_id=None,
            sequence_hint=1,
            status=StepStatus.SUCCEEDED.value,
            step_snapshot={"type": "LOOP"},
            iteration_no=None,
        )
        child = await repo.create_step(
            execution_id=execution.id,
            step_key="tool-1",
            step_type="TOOL",
            mcp_tool_version_id=None,
            parent_step_id=parent.id,
            sequence_hint=2,
            status=StepStatus.SUCCEEDED.value,
            step_snapshot={"type": "TOOL"},
            iteration_no=0,
        )
        # Need server/tool version for ToolCall FK
        server = await MCPServerRepository(session).create(
            code=f"s-{uuid.uuid4().hex[:8]}",
            name="S",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name="t",
            display_name="t",
            tags=[],
            status="ACTIVE",
        )
        tv = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="d",
            input_schema={"type": "object"},
        )
        child.mcp_tool_version_id = tv.id
        base = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
        attempt = await repo.create_attempt(
            step_execution_id=child.id,
            attempt_no=1,
            status=StepAttemptStatus.SUCCEEDED.value,
            worker_id="worker-1",
            lease_expires_at=base + timedelta(minutes=1),
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            request_snapshot={"secret": "x"},
            started_at=base,
        )
        attempt.finished_at = base + timedelta(seconds=3)
        attempt.error_layer = None
        tc = await repo.create_tool_call(
            step_attempt_id=attempt.id,
            mcp_server_id=server.id,
            mcp_tool_version_id=tv.id,
            protocol_era="CURRENT",
            protocol_version="2026-07-28",
            transport_type="STREAMABLE_HTTP",
            remote_request_id=f"rr-{uuid.uuid4().hex[:8]}",
            request_meta={"Authorization": "Bearer x"},
            normalized_status=ToolCallNormalizedStatus.SUCCEEDED.value,
            started_at=base,
        )
        tc.response_meta = {"body": "y"}
        tc.request_bytes = 10
        tc.response_bytes = 20
        tc.first_byte_at = base + timedelta(milliseconds=100)
        tc.finished_at = base + timedelta(seconds=2)
        await session.commit()
        exec_id, step_id = execution.id, child.id

    steps = await client.get(f"{API}/{exec_id}/steps")
    assert steps.status_code == 200
    items = steps.json()["items"]
    assert [i["step_key"] for i in items] == ["loop", "tool-1"]
    child_item = items[1]
    assert child_item["parent_step_id"] == items[0]["id"]
    assert child_item["iteration_no"] == 0
    assert "step_snapshot" not in child_item
    assert "resolved_input" not in child_item

    detail = await client.get(f"{API}/{exec_id}/steps/{step_id}")
    assert detail.status_code == 200
    d = detail.json()
    assert len(d["attempts"]) == 1
    att = d["attempts"][0]
    assert att["attempt_no"] == 1
    assert "worker_id" not in att
    assert "idempotency_key" not in att
    assert "request_snapshot" not in att
    assert len(att["tool_calls"]) == 1
    tc = att["tool_calls"][0]
    assert tc["request_bytes"] == 10
    assert tc["duration_ms"] == 2000
    assert tc["time_to_first_byte_ms"] == 100
    assert "request_meta" not in tc
    assert "response_meta" not in tc
    assert "remote_request_id" not in tc

    wrong = await client.get(f"{API}/{uuid.uuid4()}/steps/{step_id}")
    assert wrong.status_code == 404


@pytest.mark.asyncio
async def test_inverted_window_and_literal_q_wildcard(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, user_id = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=True
    )
    base = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    async with db_session_factory() as session:
        match = await seed_execution(
            session,
            requester_id=user_id,
            requested_at=base,
            error_code="PCT%FAIL",
            trace_id="literal-pct-%-mark",
        )
        other = await seed_execution(
            session,
            requester_id=user_id,
            requested_at=base + timedelta(seconds=1),
            error_code="OTHER",
            trace_id="no-wildcard-here",
        )
        await session.commit()

    equal = await client.get(
        API,
        params={
            "from": base.isoformat().replace("+00:00", "Z"),
            "to": base.isoformat().replace("+00:00", "Z"),
        },
    )
    assert equal.status_code == 422
    assert equal.json()["error"]["code"] == "VALIDATION_ERROR"

    inverted = await client.get(
        API,
        params={
            "from": (base + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "to": base.isoformat().replace("+00:00", "Z"),
        },
    )
    assert inverted.status_code == 422
    assert inverted.json()["error"]["code"] == "VALIDATION_ERROR"

    # q=% must be a literal percent search, not match-all.
    q_pct = await client.get(API, params={"q": "%", "page_size": 50})
    assert q_pct.status_code == 200
    ids = {i["id"] for i in q_pct.json()["items"]}
    assert str(match.id) in ids
    assert str(other.id) not in ids


@pytest.mark.asyncio
async def test_full_filter_contract_and_invalid_enums(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import (
        AgentStatus,
        AgentVersionStatus,
        AgentVersionValidationStatus,
        ExecutionSourceType,
        ScheduleMisfirePolicy,
        ScheduleOverlapPolicy,
        ScheduleTargetType,
        ScheduleType,
        StepStatus,
    )
    from app.repositories.agent import AgentRepository
    from app.repositories.agent_version import AgentVersionRepository
    from app.repositories.execution import ExecutionRepository
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository
    from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
    from app.repositories.workflow import WorkflowRepository
    from app.services.schedule import ScheduleService
    from tests.unit.test_schedule_service import (
        _schedule_body,
        _seed_schedule_manager,
        _seed_workflow_target,
    )

    client, user_id = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=True
    )
    async with db_session_factory() as session:
        other_user = await seed_user(session, with_execution_read=False)
        owner = await _seed_schedule_manager(session, with_workflow_execute=True)
        wf_id, wv_id = await _seed_workflow_target(session, owner)
        wf = await WorkflowRepository(session).get(wf_id)
        assert wf is not None

        agent = await AgentRepository(session).create(
            code=f"agt-{uuid.uuid4().hex[:8]}",
            name="Filter Agent",
            owner_id=user_id,
            status=AgentStatus.ACTIVE.value,
        )
        av = await AgentVersionRepository(session).create(
            agent_id=agent.id,
            version_no=1,
            system_instruction="run",
            llm_profile_id=uuid.uuid4(),
            request_schema_version="1.0",
            plan_schema_version="1.0",
            selection_settings={},
            planning_settings={},
            response_settings={},
            content_hash=uuid.uuid4().hex,
        )
        av.status = AgentVersionStatus.PUBLISHED.value
        av.validation_status = AgentVersionValidationStatus.VALID.value

        body = _schedule_body(
            target_type=ScheduleTargetType.WORKFLOW_VERSION,
            target_id=wv_id,
            schedule_type=ScheduleType.INTERVAL,
            schedule_expression="PT1H",
            timezone="UTC",
            misfire_policy=ScheduleMisfirePolicy.SKIP,
            overlap_policy=ScheduleOverlapPolicy.ALLOW,
        )
        sch = await ScheduleService(session).create(body, owner_id=owner)
        occ = await ScheduleOccurrenceRepository(session).create_planned(
            sch.id, datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
        )

        server = await MCPServerRepository(session).create(
            code=f"s-{uuid.uuid4().hex[:8]}",
            name="S",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name="echo",
            display_name="echo",
            tags=[],
            status="ACTIVE",
        )
        tv = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="d",
            input_schema={"type": "object"},
        )

        parent = await seed_execution(
            session,
            requester_id=user_id,
            source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
            trigger_type=ExecutionTriggerType.USER.value,
            workflow_version_id=wv_id,
            error_code="PARENT_CODE",
        )
        child = await seed_execution(
            session,
            requester_id=other_user,
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type=ExecutionTriggerType.RETRY.value,
            parent_execution_id=parent.id,
            error_code="CHILD_CODE",
        )
        # SQLite create_all omits Alembic lineage CHECKs.
        agent_exec = await seed_execution(
            session,
            requester_id=user_id,
            source_type=ExecutionSourceType.AGENT_REQUEST.value,
            trigger_type=ExecutionTriggerType.USER.value,
            agent_version_id=av.id,
            agent_request_id=uuid.uuid4(),
        )
        sched_exec = await seed_execution(
            session,
            requester_id=user_id,
            source_type=ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
            trigger_type=ExecutionTriggerType.SCHEDULE.value,
            workflow_version_id=wv_id,
            schedule_occurrence_id=occ.id,
        )
        repo = ExecutionRepository(session)
        await repo.create_step(
            execution_id=child.id,
            step_key="t1",
            step_type="TOOL",
            mcp_tool_version_id=tv.id,
            parent_step_id=None,
            sequence_hint=1,
            status=StepStatus.SUCCEEDED.value,
            step_snapshot={"type": "TOOL"},
        )
        await session.commit()
        ids = {
            "parent": parent.id,
            "child": child.id,
            "agent": agent_exec.id,
            "sched": sched_exec.id,
            "av": av.id,
            "wv": wv_id,
            "occ": occ.id,
            "tv": tv.id,
            "other": other_user,
            "agent_name": agent.name,
            "wf_name": wf.name,
            "agent_logical": agent.id,
            "wf_logical": wf_id,
        }

    bad_source = await client.get(API, params={"source_type": "NOT_A_SOURCE"})
    assert bad_source.status_code == 422
    bad_trigger = await client.get(API, params={"trigger_type": "NOPE"})
    assert bad_trigger.status_code == 422

    multi_src = await client.get(
        API,
        params={
            "source_type": "AGENT_REQUEST,WORKFLOW_VERSION",
            "page_size": 50,
        },
    )
    assert multi_src.status_code == 200
    multi_ids = {i["id"] for i in multi_src.json()["items"]}
    assert str(ids["agent"]) in multi_ids
    assert str(ids["parent"]) in multi_ids
    assert str(ids["child"]) not in multi_ids

    multi_trig = await client.get(
        API,
        params={"trigger_type": "USER,RETRY", "page_size": 50},
    )
    assert multi_trig.status_code == 200
    trig_ids = {i["id"] for i in multi_trig.json()["items"]}
    assert str(ids["parent"]) in trig_ids
    assert str(ids["child"]) in trig_ids

    by_req = await client.get(
        API, params={"requester_id": str(ids["other"]), "page_size": 50}
    )
    assert by_req.status_code == 200
    assert {i["id"] for i in by_req.json()["items"]} == {str(ids["child"])}

    by_av = await client.get(
        API, params={"agent_version_id": str(ids["av"]), "page_size": 50}
    )
    assert by_av.status_code == 200
    assert str(ids["agent"]) in {i["id"] for i in by_av.json()["items"]}

    by_wv = await client.get(
        API, params={"workflow_version_id": str(ids["wv"]), "page_size": 50}
    )
    assert by_wv.status_code == 200
    assert str(ids["parent"]) in {i["id"] for i in by_wv.json()["items"]}

    by_occ = await client.get(
        API, params={"schedule_occurrence_id": str(ids["occ"]), "page_size": 50}
    )
    assert by_occ.status_code == 200
    assert {i["id"] for i in by_occ.json()["items"]} == {str(ids["sched"])}

    by_parent = await client.get(
        API, params={"parent_execution_id": str(ids["parent"]), "page_size": 50}
    )
    assert by_parent.status_code == 200
    assert {i["id"] for i in by_parent.json()["items"]} == {str(ids["child"])}

    by_tool = await client.get(
        API, params={"tool_version_id": str(ids["tv"]), "page_size": 50}
    )
    assert by_tool.status_code == 200
    assert str(ids["child"]) in {i["id"] for i in by_tool.json()["items"]}

    by_err = await client.get(
        API, params={"error_code": "CHILD_CODE", "page_size": 50}
    )
    assert by_err.status_code == 200
    assert {i["id"] for i in by_err.json()["items"]} == {str(ids["child"])}

    detail_agent = await client.get(f"{API}/{ids['agent']}")
    assert detail_agent.status_code == 200
    src = detail_agent.json()["source"]
    assert src["type"] == "AGENT_REQUEST"
    assert src["version_id"] == str(ids["av"])
    assert src["logical_id"] == str(ids["agent_logical"])
    assert src["name"] == ids["agent_name"]

    detail_wf = await client.get(f"{API}/{ids['parent']}")
    assert detail_wf.status_code == 200
    src_wf = detail_wf.json()["source"]
    assert src_wf["type"] == "WORKFLOW_VERSION"
    assert src_wf["version_id"] == str(ids["wv"])
    assert src_wf["logical_id"] == str(ids["wf_logical"])
    assert src_wf["name"] == ids["wf_name"]

    detail_sched = await client.get(f"{API}/{ids['sched']}")
    assert detail_sched.status_code == 200
    src_sch = detail_sched.json()["source"]
    assert src_sch["type"] == "SCHEDULE_OCCURRENCE"
    assert src_sch["version_id"] == str(ids["wv"])
    assert src_sch["logical_id"] == str(ids["wf_logical"])
    assert src_sch["name"] == ids["wf_name"]


@pytest.mark.asyncio
async def test_terminal_duration_null_and_safe_result_summary(
    unauthenticated_db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import StepAttemptStatus, StepStatus, ToolCallNormalizedStatus
    from app.execution.completion import build_result_summary
    from app.repositories.execution import ExecutionRepository
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    client, user_id = await _client_for(
        unauthenticated_db_client, db_session_factory, with_execution_read=False
    )
    base = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    now = base + timedelta(minutes=5)
    async with db_session_factory() as session:
        corrupt = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.FAILED.value,
            requested_at=base,
            started_at=base,
            finished_at=None,
            error_code="TOOL_FAILED",
        )
        running = await seed_execution(
            session,
            requester_id=user_id,
            status=ExecutionStatus.RUNNING.value,
            requested_at=base,
            started_at=base,
            finished_at=None,
        )
        server = await MCPServerRepository(session).create(
            code=f"s-{uuid.uuid4().hex[:8]}",
            name="S",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            status="ACTIVE",
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name="t",
            display_name="t",
            tags=[],
            status="ACTIVE",
        )
        tv = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="d",
            input_schema={"type": "object"},
        )
        repo = ExecutionRepository(session)
        step = await repo.create_step(
            execution_id=corrupt.id,
            step_key="tool-1",
            step_type="TOOL",
            mcp_tool_version_id=tv.id,
            parent_step_id=None,
            sequence_hint=1,
            status=StepStatus.FAILED.value,
            step_snapshot={"type": "TOOL"},
        )
        step.started_at = base
        step.finished_at = None
        step.result_inline = {
            "secret": "should-not-leak",
            "structured_content": {"token": "abc"},
            "metadata": {"x": 1},
        }
        attempt = await repo.create_attempt(
            step_execution_id=step.id,
            attempt_no=1,
            status=StepAttemptStatus.FAILED.value,
            worker_id="w",
            lease_expires_at=base + timedelta(minutes=1),
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            request_snapshot={},
            started_at=base,
        )
        attempt.finished_at = None
        tc = await repo.create_tool_call(
            step_attempt_id=attempt.id,
            mcp_server_id=server.id,
            mcp_tool_version_id=tv.id,
            protocol_era="CURRENT",
            protocol_version="2026-07-28",
            transport_type="STREAMABLE_HTTP",
            remote_request_id=f"rr-{uuid.uuid4().hex[:8]}",
            request_meta={},
            normalized_status=ToolCallNormalizedStatus.FAILED.value,
            started_at=base,
        )
        tc.finished_at = None
        tc.first_byte_at = base - timedelta(seconds=1)  # invalid order
        # Safe summary only — not the Tool payload.
        corrupt.result_summary = build_result_summary(
            status=ExecutionStatus.FAILED.value,
            steps=[step],
            plan=None,
        )
        await session.commit()
        corrupt_id, running_id, step_id = corrupt.id, running.id, step.id

    # Inject now via service for deterministic active duration.
    from app.services.execution_query import ExecutionListQuery, ExecutionQueryService

    async with db_session_factory() as session:
        listed = await ExecutionQueryService(session).list_executions(
            actor_user_id=user_id,
            query=ExecutionListQuery(page_size=50),
            now=now,
        )
        by_id = {i.id: i for i in listed.items}
        assert by_id[corrupt_id].duration_ms is None
        assert by_id[running_id].duration_ms == 300_000

        detail = await ExecutionQueryService(session).get_execution(
            actor_user_id=user_id, execution_id=corrupt_id, now=now
        )
        assert detail.duration_ms is None
        assert detail.result_summary is not None
        dumped = str(detail.result_summary)
        assert "should-not-leak" not in dumped
        assert "structured_content" not in dumped
        assert "token" not in dumped

        step_detail = await ExecutionQueryService(session).get_step(
            actor_user_id=user_id,
            execution_id=corrupt_id,
            step_execution_id=step_id,
            now=now,
        )
        assert step_detail.duration_ms is None
        assert step_detail.attempts[0].duration_ms is None
        assert step_detail.attempts[0].tool_calls[0].duration_ms is None
        assert step_detail.attempts[0].tool_calls[0].time_to_first_byte_ms is None

    # HTTP detail also omits Tool payload keys
    http = await client.get(f"{API}/{corrupt_id}")
    assert http.status_code == 200
    body = http.json()
    assert body["duration_ms"] is None
    assert "should-not-leak" not in str(body)
    assert "result_inline" not in body
    assert "input_snapshot" not in body
    assert "policy_snapshot" not in body
    assert "plan_snapshot" not in body
