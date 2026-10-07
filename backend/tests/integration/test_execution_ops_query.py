"""PostgreSQL integration tests for Execution ops read + Dashboard aggregates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.enums import (
    ApprovalStatus,
    ExecutionSourceType,
    ExecutionStatus,
    ExecutionTriggerType,
    MCPServerStatus,
    MCPToolStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleStatus,
    ScheduleTargetType,
    ScheduleType,
    StepStatus,
)
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.schedule import ScheduleRepository
from app.services.execution_query import ExecutionListQuery, ExecutionQueryService
from app.services.operations import OperationsService
from app.services.schedule import ScheduleService
from tests.helpers.execution_ops import seed_execution, seed_user
from tests.unit.test_schedule_service import (
    _schedule_body,
    _seed_schedule_manager,
    _seed_workflow_target,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_pg_visibility_tool_filter_and_step_counts(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user_a = await seed_user(session, with_execution_read=False)
        user_b = await seed_user(session, with_execution_read=False)
        operator = await seed_user(session, with_execution_read=True)

        server = await MCPServerRepository(session).create(
            code=f"ops-{uuid.uuid4().hex[:8]}",
            name="Ops Server",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://mcp.test/mcp",
            status=MCPServerStatus.ACTIVE.value,
        )
        tools = MCPToolRepository(session)
        tool = await tools.create_tool(
            mcp_server_id=server.id,
            remote_name="echo",
            display_name="echo",
            tags=[],
            status=MCPToolStatus.ACTIVE.value,
        )
        tv = await tools.create_version(
            mcp_tool_id=tool.id,
            version_no=1,
            content_hash=uuid.uuid4().hex,
            validation_status="VALID",
            remote_description="d",
            input_schema={"type": "object"},
        )

        exec_a = await seed_execution(
            session,
            requester_id=user_a,
            status=ExecutionStatus.SUCCEEDED.value,
        )
        exec_b = await seed_execution(
            session,
            requester_id=user_b,
            status=ExecutionStatus.FAILED.value,
            error_code="WEIRD_UNKNOWN",
        )
        repo = ExecutionRepository(session)
        await repo.create_step(
            execution_id=exec_a.id,
            step_key="t1",
            step_type="TOOL",
            mcp_tool_version_id=tv.id,
            parent_step_id=None,
            sequence_hint=1,
            status=StepStatus.SUCCEEDED.value,
            step_snapshot={"type": "TOOL"},
        )
        await repo.create_step(
            execution_id=exec_a.id,
            step_key="t2",
            step_type="TOOL",
            mcp_tool_version_id=tv.id,
            parent_step_id=None,
            sequence_hint=2,
            status=StepStatus.FAILED.value,
            step_snapshot={"type": "TOOL"},
        )
        await repo.create_step(
            execution_id=exec_a.id,
            step_key="skip",
            step_type="CONDITION",
            mcp_tool_version_id=None,
            parent_step_id=None,
            sequence_hint=3,
            status=StepStatus.SKIPPED.value,
            step_snapshot={"type": "CONDITION"},
        )
        await session.commit()

        own = await ExecutionQueryService(session).list_executions(
            actor_user_id=user_a,
            query=ExecutionListQuery(page_size=50),
        )
        assert own.total == 1
        assert own.items[0].id == exec_a.id
        assert own.items[0].step_count == 3
        assert own.items[0].completed_step_count == 2
        assert own.items[0].failed_step_count == 1

        tool_list = await ExecutionQueryService(session).list_executions(
            actor_user_id=operator,
            query=ExecutionListQuery(tool_version_id=tv.id, page_size=50),
        )
        matched = [i for i in tool_list.items if i.id == exec_a.id]
        assert len(matched) == 1

        with pytest.raises(AppError) as exc:
            await ExecutionQueryService(session).get_execution(
                actor_user_id=user_a, execution_id=exec_b.id
            )
        assert exc.value.status_code == 404

        op_detail = await ExecutionQueryService(session).get_execution(
            actor_user_id=operator, execution_id=exec_b.id
        )
        assert op_detail.error_category == "unknown"


@pytest.mark.asyncio
async def test_pg_dashboard_metrics_and_aggregates(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 10, 7, 18, 0, 0, tzinfo=UTC)
    window_from = now - timedelta(hours=24)

    async with integration_session_factory() as session:
        operator = await seed_user(session, with_execution_read=True)
        owner = await _seed_schedule_manager(session, with_workflow_execute=True)
        _wf, version_id = await _seed_workflow_target(session, owner)

        for st in (
            MCPServerStatus.ACTIVE,
            MCPServerStatus.INACTIVE,
            MCPServerStatus.ERROR,
            MCPServerStatus.DRAFT,
        ):
            await MCPServerRepository(session).create(
                code=f"ms-{st.value}-{uuid.uuid4().hex[:6]}",
                name=st.value,
                transport_type="STREAMABLE_HTTP",
                endpoint_url="https://x.test",
                status=st.value,
            )
        deleted = await MCPServerRepository(session).create(
            code=f"ms-del-{uuid.uuid4().hex[:6]}",
            name="deleted",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://x.test",
            status=MCPServerStatus.ACTIVE.value,
        )
        deleted.deleted_at = now

        tools_repo = MCPToolRepository(session)
        live_server = await MCPServerRepository(session).create(
            code=f"ms-tools-{uuid.uuid4().hex[:6]}",
            name="tools",
            transport_type="STREAMABLE_HTTP",
            endpoint_url="https://x.test",
            status=MCPServerStatus.ACTIVE.value,
        )
        for st in (
            MCPToolStatus.ACTIVE,
            MCPToolStatus.MISSING,
            MCPToolStatus.BLOCKED,
            MCPToolStatus.DISCOVERED,
            MCPToolStatus.INACTIVE,
        ):
            await tools_repo.create_tool(
                mcp_server_id=live_server.id,
                remote_name=f"t-{st.value}-{uuid.uuid4().hex[:4]}",
                display_name=st.value,
                tags=[],
                status=st.value,
            )

        policy = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Ops",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope=None,
            allow_self_approval=True,
            reject_comment_required=False,
        )
        wait_exec = await seed_execution(
            session,
            requester_id=operator,
            status=ExecutionStatus.WAITING_APPROVAL.value,
            requested_at=window_from + timedelta(hours=3),
        )
        repo = ExecutionRepository(session)
        step1 = await repo.create_step(
            execution_id=wait_exec.id,
            step_key="approve-1",
            step_type="APPROVAL",
            mcp_tool_version_id=None,
            parent_step_id=None,
            sequence_hint=1,
            status=StepStatus.WAITING_APPROVAL.value,
            step_snapshot={"type": "APPROVAL"},
        )
        step2 = await repo.create_step(
            execution_id=wait_exec.id,
            step_key="approve-2",
            step_type="APPROVAL",
            mcp_tool_version_id=None,
            parent_step_id=None,
            sequence_hint=2,
            status=StepStatus.WAITING_APPROVAL.value,
            step_snapshot={"type": "APPROVAL"},
        )
        appr = ApprovalRequestRepository(session)
        await appr.create_pending(
            execution_id=wait_exec.id,
            step_execution_id=step1.id,
            approval_policy_id=policy.id,
            decision_mode=policy.decision_mode,
            required_approvals=1,
            approval_scope={},
            context_snapshot={"safe": True},
            context_hash="b" * 64,
            requested_at=now - timedelta(minutes=10),
            expires_at=now + timedelta(hours=1),
            requested_by=operator,
        )
        await appr.create_pending(
            execution_id=wait_exec.id,
            step_execution_id=step2.id,
            approval_policy_id=policy.id,
            decision_mode=policy.decision_mode,
            required_approvals=1,
            approval_scope={},
            context_snapshot={"safe": True},
            context_hash="c" * 64,
            requested_at=now - timedelta(hours=2),
            expires_at=now - timedelta(minutes=5),
            requested_by=operator,
        )

        body = _schedule_body(
            target_type=ScheduleTargetType.WORKFLOW_VERSION,
            target_id=version_id,
            schedule_type=ScheduleType.INTERVAL,
            schedule_expression="PT1H",
            timezone="UTC",
            misfire_policy=ScheduleMisfirePolicy.SKIP,
            overlap_policy=ScheduleOverlapPolicy.ALLOW,
        )
        sch = ScheduleService(session)
        s_active = await sch.create(body, owner_id=owner)
        await sch.activate(s_active.id, owner_id=owner)
        locked = await ScheduleRepository(session).get(s_active.id)
        assert locked is not None
        locked.next_run_at = now - timedelta(hours=2)

        await sch.create(body, owner_id=owner)
        s_err = await sch.create(body, owner_id=owner)
        err_row = await ScheduleRepository(session).get(s_err.id)
        assert err_row is not None
        err_row.status = ScheduleStatus.ERROR.value

        await session.commit()

        # Unique window per run so shared PG rows from prior tests cannot collide.
        # Encode entropy in the minute/second fields (stable within the hour span).
        nonce = uuid.uuid4().int % 10_000_000
        iso_from = datetime(2100, 1, 1, tzinfo=UTC) + timedelta(seconds=nonce * 3600)
        iso_now = iso_from + timedelta(hours=24)
        for status, seconds in (
            (ExecutionStatus.SUCCEEDED.value, 10),
            (ExecutionStatus.PARTIALLY_SUCCEEDED.value, 20),
            (ExecutionStatus.FAILED.value, 30),
            (ExecutionStatus.CANCELLED.value, 40),
            (ExecutionStatus.TIMED_OUT.value, 50),
            (ExecutionStatus.RUNNING.value, None),
            (ExecutionStatus.WAITING_INPUT.value, None),
            (ExecutionStatus.WAITING_APPROVAL.value, None),
        ):
            started = iso_from + timedelta(hours=2)
            finished = (
                started + timedelta(seconds=seconds) if seconds is not None else None
            )
            await seed_execution(
                session,
                requester_id=operator,
                status=status,
                source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
                trigger_type=ExecutionTriggerType.USER.value,
                workflow_version_id=version_id,
                requested_at=iso_from + timedelta(hours=1),
                started_at=started,
                finished_at=finished,
                error_code=(
                    "NETWORK_BLIP" if status == ExecutionStatus.FAILED.value else None
                ),
            )
        await session.commit()

        summary = await OperationsService(session).dashboard_summary(
            actor_user_id=operator,
            from_time=iso_from,
            to_time=iso_now,
            recent_limit=5,
            now=iso_now,
        )
        assert summary.executions.total == 8
        assert summary.executions.succeeded == 1
        assert summary.executions.partially_succeeded == 1
        assert summary.executions.failed == 1
        assert summary.executions.cancelled == 1
        assert summary.executions.timed_out == 1
        assert summary.executions.running == 1
        assert summary.executions.waiting_input == 1
        assert summary.executions.waiting_approval == 1
        assert summary.terminal_total == 5
        assert summary.success_rate == pytest.approx(1 / 5)
        # Deterministic 10s..50s → avg=30000; p50=30000;
        # PG percentile_cont(0.95): pos=1+0.95*4=4.8 → 40000+0.8*10000=48000; max=50000
        assert summary.avg_duration_ms == pytest.approx(30_000.0)
        assert summary.p95_duration_ms == pytest.approx(48_000.0)
        assert summary.approvals.pending >= 2
        assert summary.approvals.overdue >= 1
        assert summary.schedules.active >= 1
        assert summary.schedules.error >= 1
        assert summary.schedules.overdue >= 1
        assert summary.mcp_servers.active >= 1
        assert summary.mcp_servers.draft >= 1
        assert summary.mcp_tools.problematic >= 2
        dumped = summary.model_dump_json()
        assert "https://x.test" not in dumped

        stats = await OperationsService(session).execution_stats(
            actor_user_id=operator,
            from_time=iso_from,
            to_time=iso_now,
            now=iso_now,
        )
        assert stats.by_error_category.get("network", 0) == 1
        assert stats.duration.avg_ms == pytest.approx(30_000.0)
        assert stats.duration.p50_ms == pytest.approx(30_000.0)
        assert stats.duration.p95_ms == pytest.approx(48_000.0)
        assert stats.duration.max_ms == pytest.approx(50_000.0)

        async def _ping_ok() -> bool:
            return True

        health = await OperationsService(
            session, database_ping=_ping_ok
        ).system_health(actor_user_id=operator, now=iso_now)
        assert health.database.status == "ok"
        assert health.scheduler.error_schedule_count >= 1
        assert health.scheduler.overdue_schedule_count >= 1
        assert "failed_count" not in health.outbox.model_dump()

        async def _ping_fail() -> bool:
            return False

        unhealthy = await OperationsService(
            session, database_ping=_ping_fail
        ).system_health(actor_user_id=operator, now=iso_now)
        assert unhealthy.database.status == "unavailable"


@pytest.mark.asyncio
async def test_pg_nulls_last_sort_and_source_projection(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.repositories.agent import AgentRepository
    from app.repositories.agent_version import AgentVersionRepository
    from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
    from app.repositories.workflow import WorkflowRepository
    from tests.integration.test_execution_creation import _create, _seed_ready

    nonce = uuid.uuid4().int % 10_000_000
    t0 = datetime(2101, 1, 1, tzinfo=UTC) + timedelta(seconds=nonce * 3600)
    t1 = t0 + timedelta(hours=1)
    t2 = t0 + timedelta(hours=2)

    async with integration_session_factory() as session:
        operator = await seed_user(session, with_execution_read=True)
        owner = await _seed_schedule_manager(session, with_workflow_execute=True)
        wf_id, wv_id = await _seed_workflow_target(session, owner)
        wf = await WorkflowRepository(session).get(wf_id)
        assert wf is not None

        # Nullable started_at / finished_at ordering
        e_null = await seed_execution(
            session,
            requester_id=operator,
            status=ExecutionStatus.CREATED.value,
            requested_at=t0,
            started_at=None,
            finished_at=None,
        )
        e_t1 = await seed_execution(
            session,
            requester_id=operator,
            status=ExecutionStatus.SUCCEEDED.value,
            requested_at=t0 + timedelta(seconds=1),
            started_at=t1,
            finished_at=t1 + timedelta(seconds=5),
        )
        e_t2 = await seed_execution(
            session,
            requester_id=operator,
            status=ExecutionStatus.SUCCEEDED.value,
            requested_at=t0 + timedelta(seconds=2),
            started_at=t2,
            finished_at=t2 + timedelta(seconds=5),
        )

        # WORKFLOW_VERSION source
        wf_exec = await seed_execution(
            session,
            requester_id=operator,
            source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
            trigger_type=ExecutionTriggerType.USER.value,
            workflow_version_id=wv_id,
            requested_at=t0 + timedelta(seconds=3),
        )

        # SCHEDULE_OCCURRENCE (workflow-backed)
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
        occ = await ScheduleOccurrenceRepository(session).create_planned(sch.id, t0)
        sched_exec = await seed_execution(
            session,
            requester_id=operator,
            source_type=ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
            trigger_type=ExecutionTriggerType.SCHEDULE.value,
            workflow_version_id=wv_id,
            schedule_occurrence_id=occ.id,
            requested_at=t0 + timedelta(seconds=4),
        )

        # AGENT_REQUEST via full creation path (satisfies PG lineage CHECKs)
        seeded = await _seed_ready(session)
        outcome = await _create(session, seeded)
        agent_exec_id = outcome.result.id
        agent_row = await ExecutionRepository(session).get(agent_exec_id)
        assert agent_row is not None
        agent_version = await AgentVersionRepository(session).get(
            agent_row.agent_version_id
        )
        assert agent_version is not None
        agent = await AgentRepository(session).get(agent_version.agent_id)
        assert agent is not None

        await session.commit()

        desc = await ExecutionQueryService(session).list_executions(
            actor_user_id=operator,
            query=ExecutionListQuery(
                sort="-started_at",
                page_size=50,
                from_time=t0,
                to_time=t0 + timedelta(minutes=1),
            ),
        )
        # Window only includes null/t1/t2/wf/sched (agent_exec has wall-clock requested_at)
        started_sorted = [
            i.id
            for i in desc.items
            if i.id in {e_null.id, e_t1.id, e_t2.id}
        ]
        assert started_sorted == [e_t2.id, e_t1.id, e_null.id]

        asc = await ExecutionQueryService(session).list_executions(
            actor_user_id=operator,
            query=ExecutionListQuery(
                sort="started_at",
                page_size=50,
                from_time=t0,
                to_time=t0 + timedelta(minutes=1),
            ),
        )
        started_asc = [
            i.id for i in asc.items if i.id in {e_null.id, e_t1.id, e_t2.id}
        ]
        assert started_asc == [e_t1.id, e_t2.id, e_null.id]

        fin_desc = await ExecutionQueryService(session).list_executions(
            actor_user_id=operator,
            query=ExecutionListQuery(
                sort="-finished_at",
                page_size=50,
                from_time=t0,
                to_time=t0 + timedelta(minutes=1),
            ),
        )
        finished_sorted = [
            i.id
            for i in fin_desc.items
            if i.id in {e_null.id, e_t1.id, e_t2.id}
        ]
        assert finished_sorted == [e_t2.id, e_t1.id, e_null.id]

        fin_asc = await ExecutionQueryService(session).list_executions(
            actor_user_id=operator,
            query=ExecutionListQuery(
                sort="finished_at",
                page_size=50,
                from_time=t0,
                to_time=t0 + timedelta(minutes=1),
            ),
        )
        finished_asc = [
            i.id for i in fin_asc.items if i.id in {e_null.id, e_t1.id, e_t2.id}
        ]
        assert finished_asc == [e_t1.id, e_t2.id, e_null.id]

        wf_detail = await ExecutionQueryService(session).get_execution(
            actor_user_id=operator, execution_id=wf_exec.id
        )
        assert wf_detail.source is not None
        assert wf_detail.source.type == ExecutionSourceType.WORKFLOW_VERSION
        assert wf_detail.source.version_id == wv_id
        assert wf_detail.source.logical_id == wf_id
        assert wf_detail.source.name == wf.name

        sch_detail = await ExecutionQueryService(session).get_execution(
            actor_user_id=operator, execution_id=sched_exec.id
        )
        assert sch_detail.source is not None
        assert sch_detail.source.type == ExecutionSourceType.SCHEDULE_OCCURRENCE
        assert sch_detail.source.version_id == wv_id
        assert sch_detail.source.logical_id == wf_id
        assert sch_detail.source.name == wf.name

        ag_detail = await ExecutionQueryService(session).get_execution(
            actor_user_id=operator, execution_id=agent_exec_id
        )
        assert ag_detail.source is not None
        assert ag_detail.source.type == ExecutionSourceType.AGENT_REQUEST
        assert ag_detail.source.version_id == agent_row.agent_version_id
        assert ag_detail.source.logical_id == agent.id
        assert ag_detail.source.name == agent.name
