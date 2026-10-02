"""PostgreSQL integration tests for Workflow Execution creation + runtime."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.core.errors import AppError
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.lineage import assert_tool_step_lineage
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.execution.queue import ExecutionQueueService
from app.execution.runtime_preflight import assert_source_tool_executable
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.repositories.execution import ExecutionRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.schemas.workflow import WorkflowExecutionCreateRequest
from app.services.workflow_execution_creation import WorkflowExecutionCreationService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_workflow_execution_creation import (
    _activate_tool,
    _create,
    _idem_key,
    _publish_and_activate,
    _seed_authorized_workflow_user,
    _seed_ready_workflow,
    _seed_tool_version,
)
from tests.unit.test_workflow_registry import (
    _base_plan,
    _create_draft_version,
    _create_workflow,
    _tool,
)


class _StubCurrentMCPClient:
    def __init__(self, *, result: NormalizedToolResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        from datetime import UTC, datetime

        return self._result, {"http_status": 200}, datetime.now(UTC)


def _resolver_factory() -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


async def _claim_workflow_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ctx: dict[str, Any],
    worker_id: str = "pg-wf-worker",
) -> tuple[uuid.UUID, str, uuid.UUID]:
    async with session_factory() as session:
        outcome = await _create(session, ctx, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()

    async with session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            worker_id=worker_id,
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        return execution_id, worker_id, claim.lease_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_create_persists_validation_evidence(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        ctx = await _seed_ready_workflow(session)
        outcome = await _create(session, ctx, idempotency_key=_idem_key())
        assert outcome.result.status == ExecutionStatus.CREATED.value
        version = await WorkflowVersionRepository(session).get_for_workflow(
            ctx["workflow_id"], ctx["version_id"]
        )
        assert version is not None
        assert version.validation_report is not None
        assert version.validation_report.get("valid") is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_same_idempotency_key_one_execution(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        ctx = await _seed_ready_workflow(session)
        key = _idem_key()

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await WorkflowExecutionCreationService(
                    session
                ).create_from_workflow_version(
                    workflow_id=ctx["workflow_id"],
                    version_id=ctx["version_id"],
                    requester_id=ctx["requester_id"],
                    idempotency_key=key,
                    body=WorkflowExecutionCreateRequest(inputs={}),
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    successes = [r for r in results if not isinstance(r, AppError)]
    assert len(successes) >= 1
    async with integration_session_factory() as session:
        idem_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM api_idempotency_records "
                    "WHERE idempotency_key = :k"
                ),
                {"k": key},
            )
        ).scalar_one()
        assert int(idem_count) == 1
        exec_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM executions "
                    "WHERE workflow_version_id = :v"
                ),
                {"v": str(ctx["version_id"])},
            )
        ).scalar_one()
        assert int(exec_count) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_queue_and_claim_workflow_execution(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        ctx = await _seed_ready_workflow(session)
    execution_id, _worker, _lease = await _claim_workflow_execution(
        integration_session_factory, ctx=ctx
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value
        assert execution.status == ExecutionStatus.RUNNING.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_single_tool_stub_mcp_e2e(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        ctx = await _seed_ready_workflow(session)
    execution_id, worker_id, lease_token = await _claim_workflow_execution(
        integration_session_factory, ctx=ctx, worker_id="pg-mcp-worker"
    )
    stub = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            structured_content={"ok": True, "location": "Seoul"},
            content=[{"type": "text", "text": "ok"}],
            raw_size_bytes=10,
            duration_ms=1,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=stub,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_sequential_step_output_plan_materializes_two_steps(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        tv_id = await _seed_tool_version(session)
        tool_id = await _activate_tool(session, tv_id)
        workflow = await _create_workflow(session)
        plan = _base_plan(
            workflow_id=workflow.id,
            steps=[
                _tool("step_a", tool_version_id=tv_id),
                _tool(
                    "step_b",
                    tool_version_id=tv_id,
                    depends_on=["step_a"],
                    bindings={
                        "location": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "step_a",
                            "path": "/structured_content/location",
                        }
                    },
                ),
            ],
        )
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        user_id = await _seed_authorized_workflow_user(
            session, workflow_id=workflow.id, tool_ids=[tool_id]
        )
        await session.commit()
        ctx = {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
            "tool_version_id": tv_id,
        }

    async with integration_session_factory() as session:
        outcome = await _create(session, ctx, idempotency_key=_idem_key())
        steps = await ExecutionRepository(session).list_steps(outcome.result.id)
        assert len(steps) == 2
        keys = {s.step_key for s in steps}
        assert keys == {"step_a", "step_b"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_grant_drift_fails_source_preflight(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import ResourceGrantResourceType
    from app.repositories.resource_grant import ResourceGrantRepository

    async with integration_session_factory() as session:
        ctx = await _seed_ready_workflow(session)
        outcome = await _create(session, ctx, idempotency_key=_idem_key())
        execution = await ExecutionRepository(session).get(outcome.result.id)
        assert execution is not None
        steps = await ExecutionRepository(session).list_steps(execution.id)
        lineage = assert_tool_step_lineage(execution, steps[0], steps=steps)
        expected = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id=lineage.plan_step.id,
            tool_version_id=ctx["tool_version_id"],
        )
        await assert_source_tool_executable(
            session,
            execution=execution,
            tool_version_id=ctx["tool_version_id"],
            expected_policy_snapshot=expected,
            plan_timeout_seconds=30,
        )
        grants, _total = await ResourceGrantRepository(session).list_for_user(
            ctx["requester_id"], page_size=50
        )
        tool_grants = [
            g
            for g in grants
            if g.resource_type == ResourceGrantResourceType.MCP_TOOL.value
        ]
        assert tool_grants
        await ResourceGrantRepository(session).delete(tool_grants[0])
        await session.commit()

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(outcome.result.id)
        assert execution is not None
        steps = await ExecutionRepository(session).list_steps(execution.id)
        lineage = assert_tool_step_lineage(execution, steps[0], steps=steps)
        expected = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id=lineage.plan_step.id,
            tool_version_id=ctx["tool_version_id"],
        )
        with pytest.raises(AppError) as exc:
            await assert_source_tool_executable(
                session,
                execution=execution,
                tool_version_id=ctx["tool_version_id"],
                expected_policy_snapshot=expected,
                plan_timeout_seconds=30,
            )
        assert exc.value.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_mrtr_source_assert_source_accepts_workflow(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        ctx = await _seed_ready_workflow(session)
        outcome = await _create(session, ctx, idempotency_key=_idem_key())
        execution = await ExecutionRepository(session).get(outcome.result.id)
        assert execution is not None
        assert execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value
        steps = await ExecutionRepository(session).list_steps(execution.id)
        lineage = assert_tool_step_lineage(execution, steps[0], steps=steps)
        expected = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id=lineage.plan_step.id,
            tool_version_id=ctx["tool_version_id"],
        )
        authz = await assert_source_tool_executable(
            session,
            execution=execution,
            tool_version_id=ctx["tool_version_id"],
            expected_policy_snapshot=expected,
            plan_timeout_seconds=30,
        )
        assert authz.agent_grant is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_retry_max_attempts_pinned_at_creation(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        ctx = await _seed_ready_workflow(
            session, policy_kwargs={"max_attempts": 2}
        )
        outcome = await _create(session, ctx, idempotency_key=_idem_key())
        execution = await ExecutionRepository(session).get(outcome.result.id)
        assert execution is not None
        policy = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id="step_a",
            tool_version_id=ctx["tool_version_id"],
        )
        assert policy["tool_policy"]["max_attempts"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_sequential_step_output_e2e_stub_mcp(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A → B(STEP_OUTPUT) with distinct ToolVersions; both MCP calls succeed."""
    async with integration_session_factory() as session:
        tv_a = await _seed_tool_version(session)
        tv_b = await _seed_tool_version(session)
        tool_a = await _activate_tool(session, tv_a)
        tool_b = await _activate_tool(session, tv_b)
        workflow = await _create_workflow(session)
        plan = _base_plan(
            workflow_id=workflow.id,
            steps=[
                _tool("step_a", tool_version_id=tv_a),
                _tool(
                    "step_b",
                    tool_version_id=tv_b,
                    depends_on=["step_a"],
                    bindings={
                        "location": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "step_a",
                            "path": "/structured_content/location",
                        }
                    },
                ),
            ],
        )
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        user_id = await _seed_authorized_workflow_user(
            session, workflow_id=workflow.id, tool_ids=[tool_a, tool_b]
        )
        await session.commit()
        ctx = {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
        }

    class _SeqClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def call_tool(self, endpoint, **kwargs):
            from datetime import UTC, datetime

            self.calls.append(dict(kwargs))
            n = len(self.calls)
            return (
                NormalizedToolResult(
                    protocol_success=True,
                    tool_error=False,
                    structured_content={"location": f"city-{n}", "ok": True},
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _SeqClient()
    execution_id, worker_id, lease_token = await _claim_workflow_execution(
        integration_session_factory, ctx=ctx
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    assert len(client.calls) == 2
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution.id)
        by = {s.step_key: s for s in steps}
        assert by["step_b"].resolved_input == {"location": "city-1"}
        snap = execution.policy_snapshot
        assert snap["schema_version"] == "workflow_execution_policy.v1"
        assert set(snap["tool_steps"]) == {"step_a", "step_b"}
        assert (
            snap["tool_steps"]["step_a"]["tool_version_id"]
            != snap["tool_steps"]["step_b"]["tool_version_id"]
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_foreach_workflow_body_policy_and_mcp(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import AuthorableStepType, LoopMode
    from app.models.mcp import MCPToolVersion
    from sqlalchemy import update

    async with integration_session_factory() as session:
        tv_id = await _seed_tool_version(session)
        # Body TOOL reads LOOP_CONTEXT /item (foreach projection).
        await session.execute(
            update(MCPToolVersion)
            .where(MCPToolVersion.id == tv_id)
            .values(
                input_schema={
                    "type": "object",
                    "properties": {"item": {"type": "object"}},
                    "required": ["item"],
                }
            )
        )
        tool_id = await _activate_tool(session, tv_id)
        workflow = await _create_workflow(session)
        plan = _base_plan(
            workflow_id=workflow.id,
            steps=[
                {
                    "id": "loop1",
                    "name": "loop1",
                    "type": AuthorableStepType.LOOP.value,
                    "required": True,
                    "depends_on": [],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "mode": LoopMode.FOR_EACH.value,
                        "collection": {
                            "kind": BindingKind.PLAN_INPUT.value,
                            "path": "/items",
                        },
                        "body_step_ids": ["body"],
                        "max_iterations": 10,
                    },
                },
                _tool(
                    "body",
                    tool_version_id=tv_id,
                    depends_on=["loop1"],
                    bindings={
                        "item": {
                            "kind": BindingKind.LOOP_CONTEXT.value,
                            "path": "/item",
                        }
                    },
                ),
            ],
            limits={"max_loop_iterations": 10, "max_steps": 20},
        )
        plan["inputs"] = {"items": {"type": "array", "required": True, "secret": False}}
        plan["completion"]["response_step_ids"] = ["loop1"]
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        user_id = await _seed_authorized_workflow_user(
            session, workflow_id=workflow.id, tool_ids=[tool_id]
        )
        await session.commit()
        ctx = {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
            "tool_version_id": tv_id,
        }

    class _LoopClient:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def call_tool(self, endpoint, **kwargs):
            from datetime import UTC, datetime

            self.calls.append(kwargs)
            return (
                NormalizedToolResult(
                    protocol_success=True,
                    tool_error=False,
                    structured_content={"ok": True},
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _LoopClient()
    async with integration_session_factory() as session:
        outcome = await WorkflowExecutionCreationService(
            session
        ).create_from_workflow_version(
            workflow_id=ctx["workflow_id"],
            version_id=ctx["version_id"],
            requester_id=ctx["requester_id"],
            idempotency_key=_idem_key(),
            body=WorkflowExecutionCreateRequest(
                inputs={"items": [{"id": 1}, {"id": 2}]}
            ),
        )
        execution_id = outcome.result.id
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert "body" in execution.policy_snapshot["tool_steps"]
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()

    async with integration_session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id="pg-foreach"
        )
        assert claim.claimed
        worker_id = claim.worker_id
        lease_token = claim.lease_token
        await session.commit()

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    assert len(client.calls) == 2
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution.id)
        body_rows = [s for s in steps if s.iteration_no is not None]
        assert {s.iteration_no for s in body_rows} == {1, 2}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_authorable_approval_workflow_resume(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.approval.decision import ApprovalDecisionService
    from app.domain.enums import AuthorableStepType
    from app.execution.approval_resume import ApprovalResumeClaimService
    from app.repositories.approval_policy import ApprovalPolicyRepository
    from app.repositories.approval_request import ApprovalRequestRepository

    from tests.integration.test_execution_approval_step import _grant_decide

    async with integration_session_factory() as session:
        tv_id = await _seed_tool_version(session)
        tool_id = await _activate_tool(session, tv_id)
        policy = await ApprovalPolicyRepository(session).create(
            code=f"ap-wf-{uuid.uuid4().hex[:8]}",
            name="WF Authorable",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={},
            allow_self_approval=True,
        )
        await session.flush()
        workflow = await _create_workflow(session)
        plan = _base_plan(
            workflow_id=workflow.id,
            steps=[
                _tool("step_a", tool_version_id=tv_id),
                {
                    "id": "gate",
                    "name": "gate",
                    "type": AuthorableStepType.APPROVAL.value,
                    "required": True,
                    "depends_on": ["step_a"],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {"approval_policy_id": str(policy.id)},
                },
                _tool("step_b", tool_version_id=tv_id, depends_on=["gate"]),
            ],
        )
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        user_id = await _seed_authorized_workflow_user(
            session, workflow_id=workflow.id, tool_ids=[tool_id]
        )
        await _grant_decide(session, user_id=user_id)
        await session.commit()
        ctx = {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
        }

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, endpoint, **kwargs):
            from datetime import UTC, datetime

            self.calls += 1
            return (
                NormalizedToolResult(
                    protocol_success=True,
                    tool_error=False,
                    structured_content={"ok": True, "n": self.calls},
                ),
                {"http_status": 200},
                datetime.now(UTC),
            )

    client = _Client()
    execution_id, worker_id, lease_token = await _claim_workflow_execution(
        integration_session_factory, ctx=ctx
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.workflow_version_id == ctx["version_id"]
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        approval_id = pending.id
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=ctx["requester_id"],
            decision="APPROVE",
        )
        await session.commit()

    async with integration_session_factory() as session:
        resume = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="pg-wf-resume",
        )
        assert resume.claimed
        worker_id = resume.worker_id
        lease_token = resume.lease_token
        await session.commit()

    await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.id == execution_id
        assert execution.workflow_version_id == ctx["version_id"]
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert client.calls == 2
