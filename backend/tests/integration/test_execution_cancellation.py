"""PostgreSQL integration tests for cooperative Execution cancellation (FNC-EXE-010)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    ApprovalStatus,
    AuthorableStepType,
    BindingKind,
    CURRENT_MCP_PROTOCOL_VERSION,
    ExecutionSourceType,
    ExecutionStatus,
    MCPProtocolEra,
    MCPTransportType,
    McpInputRequestStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.orchestrator import ExecutionOrchestrator
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner, _PreparedCall
from app.execution.tool_step_attempt import ToolStepAttemptService
from app.mcp.client import MCPClientError
from app.mcp.contracts import NormalizedToolResult
from app.models.approval import ApprovalDecision, ApprovalRequest
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.execution_cancellation import ExecutionCancellationService
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from app.services.role import RoleService
from app.services.user import UserService

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready
from tests.integration.test_execution_recovery import (
    _claim_ready,
    _seed_ready_with_policy,
)
from tests.unit.test_mrtr_waiting_input import _MrtrClient


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


async def _grant_codes(
    session: AsyncSession, user_id: uuid.UUID, *codes: str
) -> None:
    """Add permissions via a new role without removing existing grants."""
    from app.repositories.role import UserRoleRepository

    perm_ids: list[uuid.UUID] = []
    for code in codes:
        perm = await PermissionRepository(session).get_by_code(code)
        assert perm is not None
        perm_ids.append(perm.id)
    role = await RoleService(session).create(
        RoleCreate(code=f"pg-cx-{uuid.uuid4().hex[:8]}", name="Cancel")
    )
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=perm_ids),
        expected_lock_version=1,
    )
    existing = await UserRoleRepository(session).list_role_ids(user_id)
    if role.id not in existing:
        user = await UserRepository(session).get(user_id)
        assert user is not None
        await UserService(session).replace_roles(
            user_id,
            UserRoleReplaceRequest(role_ids=[*existing, role.id]),
            expected_lock_version=int(user.lock_version),
        )
    await session.commit()


async def _grant_cancel(session: AsyncSession, user_id: uuid.UUID) -> None:
    await _grant_codes(session, user_id, "execution.cancel")


def _tool(sid: str, *, depends_on: list[str] | None = None) -> dict[str, Any]:
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


def _plan(tool_version_id: uuid.UUID, steps: list[dict[str, Any]]) -> dict[str, Any]:
    rewritten = []
    for step in steps:
        s = dict(step)
        cfg = dict(s.get("config") or {})
        cfg["tool_version_id"] = str(tool_version_id)
        s["config"] = cfg
        rewritten.append(s)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "pg cancel",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


async def _materialize_claim(
    session: AsyncSession,
    *,
    step_specs: list[dict[str, Any]],
    worker_id: str = "pg-cancel",
    max_parallelism: int = 4,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    seeded = await _seed_ready(session)
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)
    plan = _plan(seeded["tool_version_id"], step_specs)
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = max_parallelism
    plan["limits"] = limits
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


class _BlockingClient:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                content=[{"type": "text", "text": "ok"}],
                raw_size_bytes=4,
                duration_ms=1,
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _UnknownOutcomeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        raise MCPClientError(
            error_layer="TIMEOUT",
            error_code="MCP_CONNECTION_TIMEOUT",
            message="timeout after send",
            retryable=True,
            outcome_unknown=True,
        )


class _FailOnceClient:
    def __init__(self) -> None:
        self.calls = 0
        self.after_first = asyncio.Event()

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        if self.calls == 1:
            self.after_first.set()
            raise MCPClientError(
                error_layer="NETWORK",
                error_code="MCP_NETWORK_ERROR",
                message="connect fail",
                retryable=True,
                outcome_unknown=False,
            )
        return (
            NormalizedToolResult(protocol_success=True, tool_error=False),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _StubSuccess:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls += 1
        return (
            NormalizedToolResult(protocol_success=True, tool_error=False),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _NeverCalled:
    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("MCP must not be called")


def _install_presend_seam(monkeypatch: pytest.MonkeyPatch, runner: McpToolRunner, mutate):
    async def _seam(prepared: _PreparedCall) -> None:
        async with runner._session_factory() as session:
            await mutate(session, prepared)
            await session.commit()

    monkeypatch.setattr(runner, "_after_phase_a_before_final_gate", _seam)


# ---------------------------------------------------------------------------
# PG A — queue/cancel race
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_a_queue_cancel_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        created = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = created.result.id
        requester_id = seeded["requester_id"]
        await _grant_cancel(session, requester_id)

    async def _cancel() -> str:
        async with integration_session_factory() as session:
            outcome = await ExecutionCancellationService(session).request_user_cancel(
                execution_id, actor_user_id=requester_id, reason="race"
            )
            return outcome.status

    async def _stage() -> int:
        async with integration_session_factory() as session:
            n = await ExecutionQueueService(session).stage_created_batch(limit=10)
            await session.commit()
            return n

    results = await asyncio.gather(_cancel(), _stage())
    cancel_status, staged = results
    assert cancel_status == ExecutionStatus.CANCELLED.value

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED.value
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="late"
        )
        assert claim.claimed is False
        # If staging won first, Outbox may exist; claim must still no-op.
        _ = staged


# ---------------------------------------------------------------------------
# PG B — claim/cancel race
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_b_claim_cancel_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        created = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = created.result.id
        requester_id = seeded["requester_id"]
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await _grant_cancel(session, requester_id)
        await session.commit()

    async def _cancel() -> str:
        async with integration_session_factory() as session:
            outcome = await ExecutionCancellationService(session).request_user_cancel(
                execution_id, actor_user_id=requester_id, reason="claim-race"
            )
            return outcome.status

    async def _claim() -> bool:
        async with integration_session_factory() as session:
            claim = await ExecutionClaimService(session, lease_seconds=60).claim(
                execution_id=execution_id, worker_id="racer"
            )
            await session.commit()
            return claim.claimed

    cancel_status, claimed = await asyncio.gather(_cancel(), _claim())
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        if claimed:
            # Claim won → RUNNING then cancel applies RUNNING rules (no ToolCall → CANCELLED)
            assert cancel_status in {
                ExecutionStatus.CANCELLED.value,
                ExecutionStatus.CANCEL_REQUESTED.value,
            }
            if cancel_status == ExecutionStatus.CANCELLED.value:
                assert execution.status == ExecutionStatus.CANCELLED.value
        else:
            assert cancel_status == ExecutionStatus.CANCELLED.value
            assert execution.status == ExecutionStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# PG C — cancel before first Attempt
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_c_cancel_before_first_attempt(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, worker_id, lease_token, _ = await _claim_ready(
        integration_session_factory, worker_id="pg-c"
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        await _grant_cancel(session, execution.requester_id)
        outcome = await ExecutionCancellationService(session).request_user_cancel(
            execution_id,
            actor_user_id=execution.requester_id,
            reason="before-attempt",
        )
        assert outcome.status == ExecutionStatus.CANCELLED.value

    client = _NeverCalled()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    result = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert result.mcp_called is False

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.CANCELLED.value
        assert await ExecutionRepository(session).list_attempts(step.id) == []


# ---------------------------------------------------------------------------
# PG D — cancel between Phase A and B2
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_d_cancel_between_phase_a_and_b2(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, worker_id, lease_token, _ = await _claim_ready(
        integration_session_factory, worker_id="pg-d"
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        requester_id = execution.requester_id
        await _grant_cancel(session, requester_id)

    client = _NeverCalled()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    async def _cancel_after_phase_a(session: AsyncSession, prepared: _PreparedCall) -> None:
        del prepared
        await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="pre-b2"
        )

    async with integration_session_factory() as session:
        step_id = (await ExecutionRepository(session).list_steps(execution_id))[0].id

    _install_presend_seam(monkeypatch, runner, _cancel_after_phase_a)
    outcome = await runner.run_claimed_tool_step(
        execution_id=execution_id,
        step_execution_id=step_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.CANCELLED.value
    assert outcome.reason == "CANCEL_REQUESTED"

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.CANCELLED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.CANCELLED.value
        tcs = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tcs) == 1
        assert tcs[0].normalized_status == ToolCallNormalizedStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# PG E — cancel after remote call begins
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_e_cancel_after_remote_begins(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, worker_id, lease_token, _ = await _claim_ready(
        integration_session_factory, worker_id="pg-e"
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        requester_id = execution.requester_id
        await _grant_cancel(session, requester_id)

    client = _BlockingClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    task = asyncio.create_task(
        runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )
    )
    await client.entered.wait()
    async with integration_session_factory() as session:
        outcome = await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="inflight"
        )
        assert outcome.status == ExecutionStatus.CANCEL_REQUESTED.value
    client.release.set()
    result = await task
    assert client.calls == 1
    assert result.mcp_called is True

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        # Known success may keep Step SUCCEEDED; Execution CANCELLED.
        assert step.status in {
            StepStatus.SUCCEEDED.value,
            StepStatus.CANCELLED.value,
        }
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1


# ---------------------------------------------------------------------------
# PG F — UNKNOWN_OUTCOME cancellation race
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_f_unknown_outcome_keeps_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
        max_attempts=1,
    )
    execution_id, worker_id, lease_token, _ = await _claim_ready(
        integration_session_factory, seeded=seeded, worker_id="pg-f"
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        requester_id = execution.requester_id
        await _grant_cancel(session, requester_id)

    client = _UnknownOutcomeClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    task = asyncio.create_task(
        runner.run_claimed_execution(
            execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
        )
    )
    await client.entered.wait()
    async with integration_session_factory() as session:
        await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="unknown-race"
        )
    client.release.set()
    await task

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.cancel_requested_at is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.UNKNOWN_OUTCOME.value
        assert client.calls == 1


# ---------------------------------------------------------------------------
# PG G — sequential no downstream
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_g_sequential_no_downstream(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, seeded = await _materialize_claim(
            session,
            step_specs=[_tool("a"), _tool("b", depends_on=["a"])],
            worker_id="pg-g",
        )
        requester_id = seeded["requester_id"]
        await _grant_cancel(session, requester_id)
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        a_id = by_key["a"].id

    client = _StubSuccess()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome_a = await runner.run_claimed_tool_step(
        execution_id=execution_id,
        step_execution_id=a_id,
        worker_id="pg-g",
        lease_token=lease_token,
        defer_execution_terminalization=True,
    )
    assert outcome_a.terminal_status == StepStatus.SUCCEEDED.value
    assert client.calls == 1

    async with integration_session_factory() as session:
        cancel = await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="no-b"
        )
        assert cancel.status == ExecutionStatus.CANCELLED.value

    # Late orchestrator must not start B / MCP.
    orchestrator = ExecutionOrchestrator(
        session_factory=integration_session_factory,
        tool_runner=runner,
    )
    late = await orchestrator.run(
        execution_id=execution_id, worker_id="pg-g", lease_token=lease_token
    )
    assert late.mcp_called is False
    assert client.calls == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["a"].status == StepStatus.SUCCEEDED.value
        assert by_key["b"].status == StepStatus.CANCELLED.value
        assert await ExecutionRepository(session).list_attempts(by_key["b"].id) == []


# ---------------------------------------------------------------------------
# PG H — parallel sibling
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_h_parallel_sibling_cancel(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease_token, seeded = await _materialize_claim(
            session,
            step_specs=[_tool("a"), _tool("b")],
            worker_id="pg-h",
            max_parallelism=2,
        )
        requester_id = seeded["requester_id"]
        await _grant_cancel(session, requester_id)
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        # Promote both to READY (materializer may leave both READY already).
        for s in steps:
            if s.status == StepStatus.PENDING.value:
                s.status = StepStatus.READY.value
                s.ready_at = datetime.now(UTC)
        await session.commit()
        a_id = by_key["a"].id

    # Start A Phase A with ToolCall STARTED, leave B READY, then cancel.
    async with integration_session_factory() as session:
        now = datetime.now(UTC)
        started = await ToolStepAttemptService(session).start(
            execution_id=execution_id,
            step_execution_id=a_id,
            worker_id="pg-h",
            lease_token=lease_token,
            now=now,
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        step_a = next(s for s in steps if s.id == a_id)
        version = await MCPToolRepository(session).get_version(step_a.mcp_tool_version_id)
        assert version is not None
        tool = await MCPToolRepository(session).get(version.mcp_tool_id)
        assert tool is not None
        await ExecutionRepository(session).create_tool_call(
            step_attempt_id=started.attempt_id,
            mcp_server_id=tool.mcp_server_id,
            mcp_tool_version_id=version.id,
            protocol_era=MCPProtocolEra.CURRENT.value,
            protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
            transport_type=MCPTransportType.STREAMABLE_HTTP.value,
            remote_request_id=str(uuid.uuid4()),
            request_meta={},
            normalized_status=ToolCallNormalizedStatus.STARTED.value,
            started_at=now,
        )
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="parallel"
        )
        assert outcome.status == ExecutionStatus.CANCEL_REQUESTED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["a"].status == StepStatus.RUNNING.value
        assert by_key["b"].status == StepStatus.CANCELLED.value
        assert await ExecutionRepository(session).list_attempts(by_key["b"].id) == []


# ---------------------------------------------------------------------------
# PG I — WAITING_APPROVAL
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_i_waiting_approval_cancel(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Cancel Gate",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={},
            allow_self_approval=True,
        )
        await session.commit()
        seeded = await _seed_ready(
            session,
            policy_requires_approval=True,
            approval_policy_id=approval.id,
        )
        created = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = created.result.id
        requester_id = seeded["requester_id"]
        await _grant_cancel(session, requester_id)

    async with integration_session_factory() as session:
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
    async with integration_session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="pg-i"
        )
        await session.commit()
        assert claim.claimed and claim.lease_token is not None
        lease_token = claim.lease_token

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=_NeverCalled(),  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="pg-i", lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value

    async with integration_session_factory() as session:
        cancel = await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="approval-wait"
        )
        assert cancel.status == ExecutionStatus.CANCELLED.value
        approval_row = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.execution_id == execution_id
                )
            )
        ).scalar_one()
        assert approval_row.status == ApprovalStatus.CANCELLED.value
        assert approval_row.resolved_at is not None
        decisions = (
            await session.execute(
                select(ApprovalDecision).where(
                    ApprovalDecision.approval_request_id == approval_row.id
                )
            )
        ).scalars().all()
        assert decisions == []
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# PG J — approved-resume race (cancel before resume claim)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_j_cancel_before_approval_resume(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.approval.decision import ApprovalDecisionService
    from app.execution.approval_resume import ApprovalResumeClaimService

    async with integration_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Resume Race",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={},
            allow_self_approval=True,
        )
        await session.commit()
        seeded = await _seed_ready(
            session,
            policy_requires_approval=True,
            approval_policy_id=approval.id,
        )
        created = await _create(session, seeded, idempotency_key=_idem_key())
        execution_id = created.result.id
        requester_id = seeded["requester_id"]
        await _grant_codes(
            session, requester_id, "execution.cancel", "approval.decide"
        )

    async with integration_session_factory() as session:
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
    async with integration_session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="pg-j"
        )
        await session.commit()
        lease_token = claim.lease_token
        assert lease_token is not None

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=_NeverCalled(),  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="pg-j", lease_token=lease_token
    )

    async with integration_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        approval_id = pending.id
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=requester_id,
            decision="APPROVE",
        )
        await session.commit()
        refreshed = await ApprovalRequestRepository(session).get(approval_id)
        assert refreshed is not None
        assert refreshed.status == ApprovalStatus.APPROVED.value

    async with integration_session_factory() as session:
        cancel = await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="before-resume"
        )
        assert cancel.status == ExecutionStatus.CANCELLED.value
        refreshed = await ApprovalRequestRepository(session).get(approval_id)
        assert refreshed is not None
        assert refreshed.status == ApprovalStatus.APPROVED.value

    async with integration_session_factory() as session:
        claimed = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="late-resume",
        )
        assert claimed.claimed is False


# ---------------------------------------------------------------------------
# PG K — WAITING_INPUT
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_k_waiting_input_cancel(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, worker_id, lease_token, _ = await _claim_ready(
        integration_session_factory, worker_id="pg-k"
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        requester_id = execution.requester_id
        await _grant_cancel(session, requester_id)

    client = _MrtrClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_INPUT.value

    async with integration_session_factory() as session:
        cancel = await ExecutionCancellationService(session).request_user_cancel(
            execution_id, actor_user_id=requester_id, reason="mrtr"
        )
        assert cancel.status == ExecutionStatus.CANCELLED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.CANCELLED.value
        reqs = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(reqs) == 1
        assert reqs[0].status == McpInputRequestStatus.REJECTED.value
        assert reqs[0].response_payload is None
        assert reqs[0].answered_at is not None
        assert reqs[0].answered_by == requester_id
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# PG L — retry suppression
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_l_retry_suppressed_by_cancel(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.READ_ONLY.value,
        max_attempts=2,
    )
    execution_id, worker_id, lease_token, _ = await _claim_ready(
        integration_session_factory, seeded=seeded, worker_id="pg-l"
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        requester_id = execution.requester_id
        await _grant_cancel(session, requester_id)
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        step_id = step.id

    client = _FailOnceClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )

    original_finalize = runner._finalize_locked

    async def _finalize_and_cancel(*args: Any, **kwargs: Any):
        result = await original_finalize(*args, **kwargs)
        if client.calls == 1 and result.reason == "SAFE_RETRY_READY":
            async with integration_session_factory() as session:
                await ExecutionCancellationService(session).request_user_cancel(
                    execution_id, actor_user_id=requester_id, reason="no-retry"
                )
        return result

    monkeypatch.setattr(runner, "_finalize_locked", _finalize_and_cancel)
    outcome = await runner.run_claimed_tool_step(
        execution_id=execution_id,
        step_execution_id=step_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    assert client.calls == 1
    assert outcome.reason == "CANCEL_REQUESTED"

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Stale worker fencing
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_stale_worker_fencing_after_cancel(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, worker_id, lease_token, _ = await _claim_ready(
        integration_session_factory, worker_id="pg-stale"
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        await _grant_cancel(session, execution.requester_id)
        await ExecutionCancellationService(session).request_user_cancel(
            execution_id,
            actor_user_id=execution.requester_id,
            reason="fence",
        )

    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=_NeverCalled(),  # type: ignore[arg-type]
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    result = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert result.mcp_called is False

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
