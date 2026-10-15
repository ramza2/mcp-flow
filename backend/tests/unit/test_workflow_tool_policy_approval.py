"""Issue #111: Workflow ToolPolicy requires_approval wait + resume."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.approval.decision import ApprovalDecisionService
from app.approval.expiry import ApprovalExpiryService
from app.core.errors import AppError
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    ApprovalStatus,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
    WorkflowStatus,
)
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.models.approval import ApprovalRequest
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.workflow import WorkflowRepository
from app.schemas.workflow import WorkflowUpdate
from app.services.workflow import WorkflowService
from app.services.workflow_version import WorkflowVersionService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_approval_step import _grant_decide
from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_workflow_execution_creation import (
    _activate_tool,
    _create,
    _idem_key,
    _publish_and_activate,
    _seed_authorized_workflow_user,
    _seed_tool_version,
)
from tests.unit.test_workflow_registry import (
    _base_plan,
    _create_draft_version,
    _create_workflow,
    _tool,
)


def _resolver_factory() -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


class _CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, endpoint, **kwargs):
        self.calls += 1
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True, "n": self.calls},
                content=[{"type": "text", "text": "ok"}],
                raw_size_bytes=8,
                duration_ms=1,
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


async def _seed_approval_workflow(
    session: AsyncSession,
    *,
    steps_factory,
) -> dict[str, Any]:
    tv_id = await _seed_tool_version(session)
    tool_id = await _activate_tool(session, tv_id)
    approval = await ApprovalPolicyRepository(session).create(
        code=f"ap-tp-{uuid.uuid4().hex[:8]}",
        name="Workflow ToolPolicy Approval",
        decision_mode="ANY",
        required_approvals=1,
        default_expiry_seconds=3600,
        approver_scope={},
        allow_self_approval=True,
    )
    await session.flush()
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(tool_id)
    assert policy is not None
    policy.requires_approval = True
    policy.approval_policy_id = approval.id
    await session.flush()

    workflow = await _create_workflow(session)
    plan = _base_plan(workflow_id=workflow.id, steps=steps_factory(tv_id))
    version = await _create_draft_version(session, workflow.id, plan=plan)
    await _publish_and_activate(session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        session, workflow_id=workflow.id, tool_ids=[tool_id]
    )
    await _grant_decide(session, user_id=user_id)
    await session.commit()
    return {
        "workflow_id": workflow.id,
        "version_id": version.id,
        "tool_id": tool_id,
        "tool_version_id": tv_id,
        "requester_id": user_id,
        "approval_policy_id": approval.id,
    }


async def _claim_created(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: dict[str, Any],
    *,
    worker_id: str = "wf-tp-worker",
) -> tuple[uuid.UUID, str, uuid.UUID]:
    async with session_factory() as session:
        outcome = await _create(session, ctx, idempotency_key=_idem_key())
        execution_id = outcome.result.id
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()

    async with session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        return execution_id, worker_id, claim.lease_token


def _runner(
    session_factory: async_sessionmaker[AsyncSession], client: Any
) -> McpToolRunner:
    return McpToolRunner(
        session_factory=session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )


@pytest.mark.asyncio
async def test_sequential_a_b_approval_c_resume_same_execution(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)

    async with db_session_factory() as session:
        tv_a = await _seed_tool_version(session)
        tool_a = await _activate_tool(session, tv_a)
        tv_b = await _seed_tool_version(session)
        tool_b = await _activate_tool(session, tv_b)
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-seq-{uuid.uuid4().hex[:8]}",
            name="Seq B Approval",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={},
            allow_self_approval=True,
        )
        await session.flush()
        pol_b = await MCPToolPolicyRepository(session).get_by_tool_id(tool_b)
        assert pol_b is not None
        pol_b.requires_approval = True
        pol_b.approval_policy_id = approval.id
        workflow = await _create_workflow(session)
        plan = _base_plan(
            workflow_id=workflow.id,
            steps=[
                _tool("a", tool_version_id=tv_a),
                _tool(
                    "b",
                    tool_version_id=tv_b,
                    depends_on=["a"],
                    bindings={
                        "location": {
                            "kind": BindingKind.LITERAL.value,
                            "value": "from-a",
                        }
                    },
                ),
                _tool("c", tool_version_id=tv_a, depends_on=["b"]),
            ],
        )
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        user_id = await _seed_authorized_workflow_user(
            session, workflow_id=workflow.id, tool_ids=[tool_a, tool_b]
        )
        await _grant_decide(session, user_id=user_id)
        await session.commit()
        ctx = {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
        }

    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    runner = _runner(db_session_factory, client)
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["a"].status == StepStatus.SUCCEEDED.value
        assert by["b"].status == StepStatus.WAITING_APPROVAL.value
        assert by["c"].status == StepStatus.PENDING.value
        assert client.calls == 1  # A only; B remote 0
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

    async with db_session_factory() as session:
        resume = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="wf-tp-resume",
        )
        assert resume.claimed
        assert resume.ready_step_id is not None
        worker_id = resume.worker_id
        lease = resume.lease_token
        await session.commit()

    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.id == execution_id
        assert execution.workflow_version_id == ctx["version_id"]
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["b"].status == StepStatus.SUCCEEDED.value
        assert by["c"].status == StepStatus.SUCCEEDED.value
        assert client.calls == 3


@pytest.mark.asyncio
async def test_dependency_barrier_approval_tool_waits_for_upstream(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        ctx = await _seed_approval_workflow(
            session,
            steps_factory=lambda tv: [
                _tool("a", tool_version_id=tv),
                _tool("b", tool_version_id=tv, depends_on=["a"]),
            ],
        )

    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["a"].status == StepStatus.WAITING_APPROVAL.value
        assert by["b"].status == StepStatus.PENDING.value
        assert client.calls == 0


@pytest.mark.asyncio
async def test_parallel_wave_deterministic_suspension_no_extra_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        tv_ok = await _seed_tool_version(session)
        tool_ok = await _activate_tool(session, tv_ok)
        tv_ap = await _seed_tool_version(session)
        tool_ap = await _activate_tool(session, tv_ap)
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-par-{uuid.uuid4().hex[:8]}",
            name="Parallel Approval",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={},
            allow_self_approval=True,
        )
        await session.flush()
        pol = await MCPToolPolicyRepository(session).get_by_tool_id(tool_ap)
        assert pol is not None
        pol.requires_approval = True
        pol.approval_policy_id = approval.id
        workflow = await _create_workflow(session)
        plan = _base_plan(
            workflow_id=workflow.id,
            steps=[
                _tool("ap", tool_version_id=tv_ap),
                _tool("free", tool_version_id=tv_ok),
            ],
            limits={"max_parallelism": 2},
        )
        version = await _create_draft_version(session, workflow.id, plan=plan)
        await _publish_and_activate(session, workflow.id, version.id)
        user_id = await _seed_authorized_workflow_user(
            session, workflow_id=workflow.id, tool_ids=[tool_ok, tool_ap]
        )
        await _grant_decide(session, user_id=user_id)
        await session.commit()
        ctx = {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
        }

    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    assert client.calls == 0
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["ap"].status == StepStatus.WAITING_APPROVAL.value
        assert by["free"].status in {
            StepStatus.READY.value,
            StepStatus.PENDING.value,
        }
        rows = list(
            (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.execution_id == execution_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == ApprovalStatus.PENDING.value


@pytest.mark.asyncio
async def test_reject_terminalizes_failed_not_rejected_status(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        ctx = await _seed_approval_workflow(
            session,
            steps_factory=lambda tv: [_tool("only", tool_version_id=tv)],
        )
    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        await ApprovalDecisionService(session).decide(
            approval_id=pending.id,
            actor_user_id=ctx["requester_id"],
            decision="REJECT",
            comment="no",
        )
        await session.commit()
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.status != "REJECTED"
        assert client.calls == 0


@pytest.mark.asyncio
async def test_expiry_terminalizes_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        ctx = await _seed_approval_workflow(
            session,
            steps_factory=lambda tv: [_tool("only", tool_version_id=tv)],
        )
    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()
        result = await ApprovalExpiryService(session).expire_due_batch(limit=10)
        await session.commit()
        assert result.expired >= 1
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        request = await ApprovalRequestRepository(session).get(pending.id)
        assert request is not None
        assert request.status == ApprovalStatus.EXPIRED.value
        assert client.calls == 0


@pytest.mark.asyncio
async def test_workflow_inactive_after_approval_blocks_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        ctx = await _seed_approval_workflow(
            session,
            steps_factory=lambda tv: [_tool("only", tool_version_id=tv)],
        )
    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
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
        wf = await WorkflowRepository(session).get(ctx["workflow_id"])
        assert wf is not None
        await WorkflowService(session).update(
            ctx["workflow_id"],
            WorkflowUpdate(
                status=WorkflowStatus.INACTIVE, lock_version=wf.lock_version
            ),
            expected_lock_version=int(wf.lock_version),
        )
        await session.commit()

    async with db_session_factory() as session:
        resume = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="wf-tp-resume",
        )
        await session.commit()
        assert resume.claimed is False
        assert resume.reason == "PRECONDITION_FAILED"
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert client.calls == 0


@pytest.mark.asyncio
async def test_newer_workflow_version_publish_keeps_pinned_resume(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        ctx = await _seed_approval_workflow(
            session,
            steps_factory=lambda tv: [_tool("only", tool_version_id=tv)],
        )
        pinned = ctx["version_id"]
    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
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
        draft = await _create_draft_version(
            session,
            ctx["workflow_id"],
            plan=_base_plan(
                workflow_id=ctx["workflow_id"],
                steps=[_tool("only", tool_version_id=ctx["tool_version_id"])],
            ),
        )
        await WorkflowVersionService(session).validate(ctx["workflow_id"], draft.id)
        await WorkflowVersionService(session).publish(ctx["workflow_id"], draft.id)
        await session.commit()

    async with db_session_factory() as session:
        resume = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="wf-tp-resume",
        )
        assert resume.claimed
        await session.commit()
        worker_id = resume.worker_id
        lease = resume.lease_token

    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.workflow_version_id == pinned
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert client.calls == 1


@pytest.mark.asyncio
async def test_tool_policy_drift_after_approval_blocks_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        ctx = await _seed_approval_workflow(
            session,
            steps_factory=lambda tv: [_tool("only", tool_version_id=tv)],
        )
    client = _CountingClient()
    execution_id, worker_id, lease = await _claim_created(db_session_factory, ctx)
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )
    async with db_session_factory() as session:
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
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(ctx["tool_id"])
        assert policy is not None
        policy.requires_approval = False
        policy.approval_policy_id = None
        await session.commit()

    async with db_session_factory() as session:
        resume = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="wf-tp-resume",
        )
        await session.commit()
        if resume.claimed:
            await _runner(db_session_factory, client).run_claimed_execution(
                execution_id=execution_id,
                worker_id=resume.worker_id,
                lease_token=resume.lease_token,
            )
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert client.calls == 0


@pytest.mark.asyncio
async def test_confirmation_still_unsupported(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        tv_id = await _seed_tool_version(session)
        tool_id = await _activate_tool(session, tv_id, requires_confirmation=True)
        workflow = await _create_workflow(session)
        plan = _base_plan(
            workflow_id=workflow.id,
            steps=[_tool("a", tool_version_id=tv_id)],
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
        }
        with pytest.raises(AppError) as exc:
            await _create(session, ctx)
        assert exc.value.code == "WORKFLOW_CONFIRMATION_UNSUPPORTED"
