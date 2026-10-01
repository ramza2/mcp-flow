"""PostgreSQL integration tests for authorable APPROVAL Step runtime (PR #49)."""

from __future__ import annotations

import asyncio
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
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
)
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.claim import ExecutionClaimService
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.orchestrator import ExecutionOrchestrator
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.models.approval import ApprovalRequest
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.role import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _seed_ready


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {
            "tool_version_id": str(uuid.uuid4()),
            "bindings": {
                "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
            },
        },
    }


def _approval(
    sid: str,
    approval_policy_id: uuid.UUID,
    *,
    depends_on: list[str] | None = None,
    on_error: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.APPROVAL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": on_error,
        "config": {"approval_policy_id": str(approval_policy_id)},
    }


def _plan(
    tool_version_id: uuid.UUID,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    rewritten = []
    for step in steps:
        s = dict(step)
        if s["type"] == AuthorableStepType.TOOL.value:
            cfg = dict(s.get("config") or {})
            cfg["tool_version_id"] = str(tool_version_id)
            s["config"] = cfg
        rewritten.append(s)
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = 4
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "pg authorable approval",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


class _ScoreClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._lock = asyncio.Lock()

    async def call_tool(self, endpoint, **kwargs):
        async with self._lock:
            self.calls.append({"tool_name": kwargs.get("tool_name")})
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True, "score": 80},
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


async def _grant_decide(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    perm = await PermissionRepository(session).get_by_code("approval.decide")
    assert perm is not None
    code = f"DECIDER_{uuid.uuid4().hex[:6].upper()}"
    role = await RoleRepository(session).create(
        code=code, name=f"Decider {code}", description=None
    )
    await RolePermissionRepository(session).replace_all(role.id, [perm.id])
    existing = await UserRoleRepository(session).list_role_ids(user_id)
    if role.id not in existing:
        await UserRoleRepository(session).replace_all(user_id, [*existing, role.id])
    await session.flush()


async def _create_policy(session: AsyncSession) -> Any:
    return await ApprovalPolicyRepository(session).create(
        code=f"ap-pg-{uuid.uuid4().hex[:8]}",
        name="PG Authorable Gate",
        decision_mode="ANY",
        required_approvals=1,
        default_expiry_seconds=3600,
        # {} rather than None — PG JSONB None can serialize as JSON null and
        # violate ck_approval_requests_approval_scope_object.
        approver_scope={},
        allow_self_approval=True,
    )


async def _materialize_claim(
    session: AsyncSession,
    *,
    step_specs: list[dict[str, Any]],
    worker_id: str = "pg-appr",
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    seeded = await _seed_ready(session)
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)
    plan = _plan(seeded["tool_version_id"], step_specs)
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan,
            plan_hash=compute_plan_hash(plan),
            input_snapshot={},
            policy_snapshot=policy_snapshot,
            requested_at=datetime.now(UTC),
        )
    )
    execution = outcome.execution
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    claim = await ExecutionClaimService(session, lease_seconds=120).claim(
        execution_id=execution.id, worker_id=worker_id
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    return execution.id, claim.lease_token, seeded


async def _list_requests(
    session: AsyncSession, execution_id: uuid.UUID
) -> list[ApprovalRequest]:
    return list(
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


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    lease: uuid.UUID,
    client: _ScoreClient,
    worker_id: str = "pg-appr",
) -> Any:
    runner = McpToolRunner(
        session_factory=session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    return await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease
    )


async def _approve_resume_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    approval_id: uuid.UUID,
    requester_id: uuid.UUID,
    client: _ScoreClient,
    worker_id: str = "pg-appr-resume",
) -> Any:
    async with session_factory() as session:
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=requester_id,
            decision="APPROVE",
        )
        await session.commit()
        claim = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id=worker_id,
        )
        await session.commit()
        assert claim.claimed is True
        assert claim.lease_token is not None
        lease = claim.lease_token
    return await _run(
        session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id=worker_id,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_happy_path_a_p_b(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        policy = await _create_policy(session)
        await session.flush()
        execution_id, lease, seeded = await _materialize_claim(
            session,
            step_specs=[
                _tool("a"),
                _approval("p", policy.id, depends_on=["a"]),
                _tool("b", depends_on=["p"]),
            ],
        )
    client = _ScoreClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
    )
    assert outcome.reason == "WAITING_APPROVAL"
    assert len(client.calls) == 1

    async with integration_session_factory() as session:
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        approval_id = pending.id
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p"].status == StepStatus.WAITING_APPROVAL.value

    outcome2 = await _approve_resume_run(
        integration_session_factory,
        execution_id=execution_id,
        approval_id=approval_id,
        requester_id=seeded["requester_id"],
        client=client,
    )
    assert outcome2.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 2
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p"].status == StepStatus.SUCCEEDED.value
        assert by["p"].result_inline == {
            "approval_status": ApprovalStatus.APPROVED.value,
            "approval_request_id": str(approval_id),
        }
        assert await ExecutionRepository(session).list_attempts(by["p"].id) == []
        assert by["b"].status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_reject_continue_ignored(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        policy = await _create_policy(session)
        await session.flush()
        execution_id, lease, seeded = await _materialize_claim(
            session,
            step_specs=[
                _approval("p", policy.id, on_error="CONTINUE"),
                _tool("b", depends_on=["p"]),
            ],
            worker_id="pg-appr-rej",
        )
    client = _ScoreClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-appr-rej",
    )
    assert outcome.reason == "WAITING_APPROVAL"
    async with integration_session_factory() as session:
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        await _grant_decide(session, user_id=seeded["requester_id"])
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=pending.id,
            actor_user_id=seeded["requester_id"],
            decision="REJECT",
            comment="blocked",
        )
        await session.commit()

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "APPROVAL_REJECTED"
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p"].error_code == "APPROVAL_REJECTED"
        assert by["b"].status == StepStatus.SKIPPED.value
        assert len(client.calls) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_expiry(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Authorable expiry is mandatory-fatal; ignores MARK_PARTIAL; cleans DAG."""
    async with integration_session_factory() as session:
        policy = await _create_policy(session)
        await session.flush()
        execution_id, lease, _seeded = await _materialize_claim(
            session,
            step_specs=[
                _tool("a"),
                _approval("p", policy.id, depends_on=["a"], on_error="MARK_PARTIAL"),
                _tool("b", depends_on=["p"]),
            ],
            worker_id="pg-appr-exp",
        )
    client = _ScoreClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-appr-exp",
    )
    assert outcome.reason == "WAITING_APPROVAL"
    assert len(client.calls) == 1
    async with integration_session_factory() as session:
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=2)
        await session.commit()
        result = await ApprovalExpiryService(session).expire_due_batch(limit=10)
        await session.commit()
        assert result.expired >= 1
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "APPROVAL_EXPIRED"
        assert execution.worker_id is None
        assert execution.lease_token is None
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["a"].status == StepStatus.SUCCEEDED.value
        assert by["p"].status == StepStatus.FAILED.value
        assert by["p"].error_code == "APPROVAL_EXPIRED"
        assert by["b"].status == StepStatus.SKIPPED.value
        assert by["b"].error_code == "UPSTREAM_EXECUTION_STOPPED"
        assert (await ExecutionRepository(session).list_attempts(by["b"].id)) == []
    assert len(client.calls) == 1  # no downstream B MCP after expiry


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_wait_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two PG sessions race authorable APPROVAL enter — one PENDING request."""
    async with integration_session_factory() as session:
        policy = await _create_policy(session)
        await session.flush()
        execution_id, lease, _seeded = await _materialize_claim(
            session,
            step_specs=[_approval("p", policy.id)],
            worker_id="pg-appr-race",
        )
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.lease_token == lease
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        approval_step = steps[0]
        assert approval_step.step_key == "p"
        assert approval_step.status == StepStatus.PENDING.value
        assert await _list_requests(session, execution_id) == []

    client = _ScoreClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory,
        tool_runner=runner,
    )
    barrier = asyncio.Barrier(2)

    async def _reconcile_once() -> Any:
        await barrier.wait()
        return await orch._prepare_wave(
            execution_id=execution_id,
            worker_id="pg-appr-race",
            lease_token=lease,
        )

    r1, r2 = await asyncio.gather(_reconcile_once(), _reconcile_once())
    reasons = {r1.reason, r2.reason}
    assert "WAITING_APPROVAL" in reasons
    # Loser may also observe durable wait after winner commits.
    assert reasons <= {"WAITING_APPROVAL", "STALE_LEASE", "NO_READY", "WAVE_READY"}

    async with integration_session_factory() as session:
        requests = await _list_requests(session, execution_id)
        assert len(requests) == 1
        pending = requests[0]
        assert pending.status == ApprovalStatus.PENDING.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert pending.step_execution_id == by["p"].id
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert by["p"].status == StepStatus.WAITING_APPROVAL.value
        assert (await ExecutionRepository(session).list_attempts(by["p"].id)) == []
        assert len(client.calls) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_simultaneous_eligible_approval_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two sessions race when P1 and P2 are both eligible — Plan-order P1 wins."""
    async with integration_session_factory() as session:
        p1_policy = await _create_policy(session)
        p2_policy = await _create_policy(session)
        await session.flush()
        execution_id, lease, _seeded = await _materialize_claim(
            session,
            step_specs=[
                _approval("p1", p1_policy.id),
                _approval("p2", p2_policy.id),
            ],
            worker_id="pg-appr-p12-race",
        )
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.lease_token == lease
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p1"].status == StepStatus.PENDING.value
        assert by["p2"].status == StepStatus.PENDING.value
        assert await _list_requests(session, execution_id) == []
        p1_id = by["p1"].id

    client = _ScoreClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=integration_session_factory,
        tool_runner=runner,
    )
    barrier = asyncio.Barrier(2)

    async def _reconcile_once() -> Any:
        await barrier.wait()
        return await orch._prepare_wave(
            execution_id=execution_id,
            worker_id="pg-appr-p12-race",
            lease_token=lease,
        )

    await asyncio.gather(_reconcile_once(), _reconcile_once())

    async with integration_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p1"].status == StepStatus.WAITING_APPROVAL.value
        assert by["p2"].status == StepStatus.PENDING.value
        requests = await _list_requests(session, execution_id)
        assert len(requests) == 1
        assert requests[0].status == ApprovalStatus.PENDING.value
        assert requests[0].step_execution_id == p1_id
        assert requests[0].step_execution_id != by["p2"].id
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.lease_token is None
        assert len(client.calls) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_resume_mcp_at_most_one(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        policy = await _create_policy(session)
        await session.flush()
        execution_id, lease, seeded = await _materialize_claim(
            session,
            step_specs=[
                _approval("p", policy.id),
                _tool("b", depends_on=["p"]),
            ],
            worker_id="pg-appr-dup",
        )
    client = _ScoreClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-appr-dup",
    )
    assert outcome.reason == "WAITING_APPROVAL"
    async with integration_session_factory() as session:
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        approval_id = pending.id
        await _grant_decide(session, user_id=seeded["requester_id"])
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=seeded["requester_id"],
            decision="APPROVE",
        )
        await session.commit()
        claim = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="pg-appr-dup-r1",
        )
        await session.commit()
        assert claim.claimed is True
        lease2 = claim.lease_token
        assert lease2 is not None
        dup = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="pg-appr-dup-r2",
        )
        await session.commit()
        assert dup.claimed is False
        assert dup.reason == "STALE_DELIVERY"

    outcome2 = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease2,
        client=client,
        worker_id="pg-appr-dup-r1",
    )
    assert outcome2.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) <= 1
    assert len(client.calls) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_multiple_approval_serialization(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        p1 = await _create_policy(session)
        p2 = await _create_policy(session)
        await session.flush()
        execution_id, lease, seeded = await _materialize_claim(
            session,
            step_specs=[
                _approval("p1", p1.id),
                _approval("p2", p2.id),
            ],
            worker_id="pg-appr-ser",
        )
    client = _ScoreClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-appr-ser",
    )
    assert outcome.reason == "WAITING_APPROVAL"
    async with integration_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p1"].status == StepStatus.WAITING_APPROVAL.value
        assert by["p2"].status == StepStatus.PENDING.value
        requests = await _list_requests(session, execution_id)
        assert len(requests) == 1
        approval_id = requests[0].id

    outcome2 = await _approve_resume_run(
        integration_session_factory,
        execution_id=execution_id,
        approval_id=approval_id,
        requester_id=seeded["requester_id"],
        client=client,
        worker_id="pg-appr-ser-r",
    )
    assert outcome2.reason == "WAITING_APPROVAL"
    async with integration_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p1"].status == StepStatus.SUCCEEDED.value
        assert by["p2"].status == StepStatus.WAITING_APPROVAL.value
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        assert pending.step_execution_id == by["p2"].id
        approval2 = pending.id

    outcome3 = await _approve_resume_run(
        integration_session_factory,
        execution_id=execution_id,
        approval_id=approval2,
        requester_id=seeded["requester_id"],
        client=client,
        worker_id="pg-appr-ser-r2",
    )
    assert outcome3.reason == "EXECUTION_SUCCEEDED"
    async with integration_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p1"].status == StepStatus.SUCCEEDED.value
        assert by["p2"].status == StepStatus.SUCCEEDED.value
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_context_drift_fail_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        policy = await _create_policy(session)
        await session.flush()
        execution_id, lease, seeded = await _materialize_claim(
            session,
            step_specs=[
                _tool("a"),
                _approval("p", policy.id, depends_on=["a"]),
                _tool("b", depends_on=["p"]),
            ],
            worker_id="pg-appr-drift",
        )
    client = _ScoreClient()
    outcome = await _run(
        integration_session_factory,
        execution_id=execution_id,
        lease=lease,
        client=client,
        worker_id="pg-appr-drift",
    )
    assert outcome.reason == "WAITING_APPROVAL"
    assert len(client.calls) == 1

    async with integration_session_factory() as session:
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        approval_id = pending.id
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        # Mutate upstream evidence after wait — decide must fail closed.
        by["a"].result_inline = {"ok": True, "score": 1, "drift": True}
        await session.commit()

        await _grant_decide(session, user_id=seeded["requester_id"])
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id,
                actor_user_id=seeded["requester_id"],
                decision="APPROVE",
            )
        assert exc.value.status_code == 409

    async with integration_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        # Decide fail-closed before mutating request status — still PENDING.
        assert request.status == ApprovalStatus.PENDING.value
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["b"].status in {
            StepStatus.PENDING.value,
            StepStatus.SKIPPED.value,
        }
        assert len(client.calls) == 1  # B never ran
