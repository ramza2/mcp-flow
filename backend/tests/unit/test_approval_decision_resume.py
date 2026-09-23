"""Approval decision aggregation + same-Execution resume (FNC-APR-003/004)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.approval.decision import (
    ApprovalDecisionService,
    aggregate_decision,
    validate_approver_scope,
)
from app.approval.evidence import find_valid_approved_evidence
from app.approval.expiry import ApprovalExpiryService
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalStatus,
    ExecutionStatus,
    RiskClass,
    StepStatus,
    UserStatus,
)
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.queue import OutboxRelayService, validate_execution_approval_resume_event
from app.execution.tool_runner import McpToolRunner
from app.models.approval import ApprovalDecision
from app.models.outbox import OutboxEvent
from app.repositories.approval_decision import ApprovalDecisionRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.role import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)
from app.repositories.user import UserRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_approval_wait import _claim_approval_required
from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_tool_runner import _NeverCalledMCPClient, _StubCurrentMCPClient
from tests.unit.test_tool_runner_security import _unimplemented_resolver_factory


async def _grant_decide(
    session: AsyncSession, *, user_id: uuid.UUID, role_code: str | None = None
) -> uuid.UUID:
    perm = await PermissionRepository(session).get_by_code("approval.decide")
    assert perm is not None
    code = role_code or f"DECIDER_{uuid.uuid4().hex[:6].upper()}"
    role = await RoleRepository(session).create(
        code=code, name=f"Decider {code}", description=None
    )
    await RolePermissionRepository(session).replace_all(role.id, [perm.id])
    existing = await UserRoleRepository(session).list_role_ids(user_id)
    if role.id not in existing:
        await UserRoleRepository(session).replace_all(user_id, [*existing, role.id])
    await session.flush()
    return role.id


async def _create_approver(
    session: AsyncSession, *, role_code: str | None = None
) -> uuid.UUID:
    user = await UserRepository(session).create(
        username=f"appr_{uuid.uuid4().hex[:8]}",
        display_name="Approver",
        email=f"appr_{uuid.uuid4().hex[:8]}@example.com",
        status=UserStatus.ACTIVE.value,
    )
    await _grant_decide(session, user_id=user.id, role_code=role_code)
    await session.flush()
    return user.id


async def _enter_waiting(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    decision_mode: str = "ANY",
    required_approvals: int = 1,
    approver_scope: dict[str, Any] | None = None,
    allow_self_approval: bool = False,
    reject_comment_required: bool = False,
    default_expiry_seconds: int = 3600,
    risk_class: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (execution_id, approval_request_id, requester_id)."""
    from tests.unit.test_tool_runner_security import _claim_ready_execution

    _install_no_side_effects(monkeypatch)
    async with session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Decide Gate",
            decision_mode=decision_mode,
            required_approvals=required_approvals,
            default_expiry_seconds=default_expiry_seconds,
            approver_scope=approver_scope,
            allow_self_approval=allow_self_approval,
            reject_comment_required=reject_comment_required,
        )
        await session.commit()
        approval_id = approval.id

    execution_id, worker_id, lease_token = await _claim_ready_execution(
        session_factory,
        seeded_kwargs={
            "policy_requires_approval": True,
            "approval_policy_id": approval_id,
        },
        risk_class=risk_class,
    )
    runner = McpToolRunner(
        session_factory=session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value

    async with session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        return execution_id, pending.id, execution.requester_id


def test_aggregate_any_all_quorum() -> None:
    class _D:
        def __init__(self, decision: str) -> None:
            self.decision = decision

    assert (
        aggregate_decision(
            mode="ANY", required_approvals=1, decisions=[_D("APPROVE")]  # type: ignore[arg-type]
        )
        == "APPROVED"
    )
    assert (
        aggregate_decision(
            mode="ANY", required_approvals=1, decisions=[_D("REJECT")]  # type: ignore[arg-type]
        )
        == "REJECTED"
    )
    assert (
        aggregate_decision(
            mode="ALL",
            required_approvals=2,
            decisions=[_D("APPROVE")],  # type: ignore[arg-type]
        )
        is None
    )
    assert (
        aggregate_decision(
            mode="ALL",
            required_approvals=2,
            decisions=[_D("APPROVE"), _D("APPROVE")],  # type: ignore[arg-type]
        )
        == "APPROVED"
    )
    assert (
        aggregate_decision(
            mode="ALL",
            required_approvals=2,
            decisions=[_D("APPROVE"), _D("REJECT")],  # type: ignore[arg-type]
        )
        == "REJECTED"
    )
    assert (
        aggregate_decision(
            mode="QUORUM",
            required_approvals=2,
            decisions=[_D("APPROVE"), _D("REJECT")],  # type: ignore[arg-type]
        )
        is None
    )
    assert (
        aggregate_decision(
            mode="QUORUM",
            required_approvals=2,
            decisions=[_D("REJECT"), _D("REJECT")],  # type: ignore[arg-type]
        )
        == "REJECTED"
    )


def test_validate_approver_scope_shapes() -> None:
    assert validate_approver_scope(None) is None
    assert validate_approver_scope({}) is None
    assert validate_approver_scope({"role_codes": ["A", "B"]}) == ["A", "B"]
    with pytest.raises(AppError) as exc:
        validate_approver_scope({"roles": ["A"]})
    assert exc.value.status_code == 409
    with pytest.raises(AppError):
        validate_approver_scope({"role_codes": [""]})
    with pytest.raises(AppError):
        validate_approver_scope({"role_codes": "A"})


@pytest.mark.asyncio
async def test_any_approve_creates_resume_outbox(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch, decision_mode="ANY"
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        outcome = await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor,
            decision="APPROVE",
        )
        await session.commit()
        assert outcome.approval_status == ApprovalStatus.APPROVED.value
        assert outcome.resume_enqueued is True
        assert outcome.execution_status == ExecutionStatus.WAITING_APPROVAL.value
        assert outcome.step_status == StepStatus.WAITING_APPROVAL.value

        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_APPROVAL_RESUME"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].aggregate_id == execution_id
        validated_exec, validated_appr = validate_execution_approval_resume_event(
            events[0]
        )
        assert validated_exec == execution_id
        assert validated_appr == approval_id


@pytest.mark.asyncio
async def test_any_reject_fails_execution(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        outcome = await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor,
            decision="REJECT",
            comment="nope",
        )
        await session.commit()
        assert outcome.approval_status == ApprovalStatus.REJECTED.value
        assert outcome.resume_enqueued is False
        assert outcome.execution_status == ExecutionStatus.FAILED.value
        assert outcome.step_status == StepStatus.FAILED.value
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.error_code == "APPROVAL_REJECTED"
        resumes = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_APPROVAL_RESUME"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert resumes == []


@pytest.mark.asyncio
async def test_all_n2_aggregation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        decision_mode="ALL",
        required_approvals=2,
    )
    async with db_session_factory() as session:
        a1 = await _create_approver(session)
        a2 = await _create_approver(session)
        await session.commit()
        mid = await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=a1, decision="APPROVE"
        )
        await session.commit()
        assert mid.approval_status == ApprovalStatus.PENDING.value
        assert mid.resume_enqueued is False
        final = await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=a2, decision="APPROVE"
        )
        await session.commit()
        assert final.approval_status == ApprovalStatus.APPROVED.value
        assert final.resume_enqueued is True


@pytest.mark.asyncio
async def test_quorum_n2_aggregation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        decision_mode="QUORUM",
        required_approvals=2,
    )
    async with db_session_factory() as session:
        a1 = await _create_approver(session)
        a2 = await _create_approver(session)
        a3 = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=a1, decision="APPROVE"
        )
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=a2, decision="REJECT"
        )
        await session.commit()
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.PENDING.value
        final = await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=a3, decision="REJECT"
        )
        await session.commit()
        assert final.approval_status == ApprovalStatus.REJECTED.value


@pytest.mark.asyncio
async def test_duplicate_same_actor_409(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        decision_mode="ALL",
        required_approvals=2,
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
        )
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
            )
        assert exc.value.status_code == 409
        assert exc.value.code == "RESOURCE_CONFLICT"
        rows = await ApprovalDecisionRepository(session).list_for_request(approval_id)
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_permission_missing_403(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=f"noperm_{uuid.uuid4().hex[:8]}",
            display_name="No Perm",
            email=f"noperm_{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE.value,
        )
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=user.id, decision="APPROVE"
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_role_scope_match_and_mismatch(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        approver_scope={"role_codes": ["APPROVER_A"]},
    )
    async with db_session_factory() as session:
        match = await _create_approver(session, role_code="APPROVER_A")
        mismatch = await _create_approver(session, role_code="APPROVER_B")
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=mismatch, decision="APPROVE"
            )
        assert exc.value.status_code == 403
        ok = await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=match, decision="APPROVE"
        )
        await session.commit()
        assert ok.approval_status == ApprovalStatus.APPROVED.value


@pytest.mark.asyncio
async def test_malformed_scope_409(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory,
        monkeypatch,
        approver_scope={"roles": ["legacy"]},
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
            )
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_self_approval_rules(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, requester = await _enter_waiting(
        db_session_factory, monkeypatch, allow_self_approval=False
    )
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester)
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=requester, decision="APPROVE"
            )
        assert exc.value.status_code == 403
        rows = await ApprovalDecisionRepository(session).list_for_request(approval_id)
        assert rows == []

    _execution_id2, approval_id2, requester2 = await _enter_waiting(
        db_session_factory, monkeypatch, allow_self_approval=True
    )
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester2)
        await session.commit()
        ok = await ApprovalDecisionService(session).decide(
            approval_id=approval_id2, actor_user_id=requester2, decision="APPROVE"
        )
        await session.commit()
        assert ok.approval_status == ApprovalStatus.APPROVED.value


@pytest.mark.asyncio
async def test_reject_comment_required(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch, reject_comment_required=True
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=actor, decision="REJECT"
            )
        assert exc.value.status_code == 422
        with pytest.raises(AppError):
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id,
                actor_user_id=actor,
                decision="REJECT",
                comment="   ",
            )
        ok = await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=actor,
            decision="REJECT",
            comment="  blocked  ",
        )
        await session.commit()
        assert ok.approval_status == ApprovalStatus.REJECTED.value
        rows = await ApprovalDecisionRepository(session).list_for_request(approval_id)
        assert rows[0].comment == "blocked"


@pytest.mark.asyncio
async def test_expired_decision_and_sweep(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch, default_expiry_seconds=1
    )
    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        request.expires_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

        actor = await _create_approver(session)
        await session.commit()

    # decide() commits EXPIRED/FAILED then raises APPROVAL_EXPIRED.
    async with db_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
            )
        assert exc.value.status_code == 409
        assert exc.value.code == "APPROVAL_EXPIRED"

    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.EXPIRED.value
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.error_code == "APPROVAL_EXPIRED"
        assert (
            await ApprovalDecisionRepository(session).list_for_request(approval_id)
        ) == []

    # Sweep path on a fresh wait
    execution_id2, approval_id2, _ = await _enter_waiting(
        db_session_factory, monkeypatch, default_expiry_seconds=1
    )
    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id2)
        assert request is not None
        request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        result = await ApprovalExpiryService(session).expire_due_batch(limit=10)
        await session.commit()
        assert result.expired >= 1
        request = await ApprovalRequestRepository(session).get(approval_id2)
        assert request is not None
        assert request.status == ApprovalStatus.EXPIRED.value
        execution = await ExecutionRepository(session).get(execution_id2)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value


@pytest.mark.asyncio
async def test_stored_context_tamper_and_current_drift(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        snapped = dict(request.context_snapshot)
        snapped["plan_hash"] = "0" * 64
        request.context_snapshot = snapped
        await session.commit()
        with pytest.raises(AppError) as exc:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
            )
        assert exc.value.status_code == 409

    execution_id2, approval_id2, _ = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()

        from app.repositories.mcp_tool import MCPToolRepository
        from app.repositories.mcp_tool_policy import MCPToolPolicyRepository

        step = (await ExecutionRepository(session).list_steps(execution_id2))[0]
        version = await MCPToolRepository(session).get_version(step.mcp_tool_version_id)
        assert version is not None
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(
            version.mcp_tool_id
        )
        assert policy is not None
        policy.risk_class = RiskClass.DESTRUCTIVE.value
        await session.commit()

        with pytest.raises(AppError) as exc2:
            await ApprovalDecisionService(session).decide(
                approval_id=approval_id2, actor_user_id=actor, decision="APPROVE"
            )
        assert exc2.value.status_code == 409


@pytest.mark.asyncio
async def test_resume_claim_fresh_lease_and_duplicate(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp.contracts import NormalizedToolResult

    execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
        )
        await session.commit()

        claim = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-worker-1",
        )
        await session.commit()
        assert claim.claimed is True
        assert claim.lease_token is not None
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.worker_id == "resume-worker-1"
        assert execution.lease_token == claim.lease_token
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.READY.value
        first_token = claim.lease_token

        dup = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-worker-2",
        )
        await session.commit()
        assert dup.claimed is False
        assert dup.reason == "STALE_DELIVERY"
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.lease_token == first_token
        assert execution.worker_id == "resume-worker-1"

    result = NormalizedToolResult(
        protocol_success=True,
        tool_error=False,
        content=[{"type": "text", "text": "ok"}],
        structured_content={"ok": True},
        raw_size_bytes=8,
        duration_ms=1,
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_StubCurrentMCPClient(result=result),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="resume-worker-1",
        lease_token=first_token,
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_broker_publish_failure_leaves_unpublished_outbox(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _ = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
        )
        await session.commit()

        class BadPublisher:
            def publish_execution(self, **_kwargs: Any) -> None:
                raise AssertionError("dispatch unexpected")

            def publish_approval_resume(self, **_kwargs: Any) -> None:
                raise ConnectionError("broker down")

        result = await OutboxRelayService(session).publish_batch(
            publisher=BadPublisher(), limit=10
        )
        await session.commit()
        assert result.failed >= 1
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.APPROVED.value
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_APPROVAL_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].published_at is None

        class GoodPublisher:
            def __init__(self) -> None:
                self.calls: list[Any] = []

            def publish_execution(self, **_kwargs: Any) -> None:
                raise AssertionError("dispatch unexpected")

            def publish_approval_resume(self, **kwargs: Any) -> None:
                self.calls.append(kwargs)

        good = GoodPublisher()
        result2 = await OutboxRelayService(session).publish_batch(
            publisher=good, limit=10
        )
        await session.commit()
        assert result2.published >= 1
        assert len(good.calls) >= 1
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_APPROVAL_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert events[0].published_at is not None


@pytest.mark.asyncio
async def test_auth_revoked_before_resume_terminalizes(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
        )
        user = await UserRepository(session).get(requester)
        assert user is not None
        user.status = UserStatus.INACTIVE.value
        await session.commit()

        outcome = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-worker",
        )
        await session.commit()
        assert outcome.claimed is False
        assert outcome.reason == "PRECONDITION_FAILED"

    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.APPROVED.value
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "APPROVAL_RESUME_PRECONDITION_FAILED"
        assert execution.worker_id is None
        assert execution.lease_token is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        assert step.error_code == "APPROVAL_RESUME_PRECONDITION_FAILED"
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []

        # Duplicate resume after failure — no reversal / no lease / no Runner.
        dup = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-worker-2",
        )
        await session.commit()
        assert dup.claimed is False
        assert dup.reason == "STALE_DELIVERY"
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        assert execution.worker_id is None


@pytest.mark.asyncio
async def test_context_drift_before_resume_terminalizes(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.mcp_tool import MCPToolRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository

    execution_id, approval_id, _requester = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        actor = await _create_approver(session)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id, actor_user_id=actor, decision="APPROVE"
        )
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        version = await MCPToolRepository(session).get_version(step.mcp_tool_version_id)
        assert version is not None
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(
            version.mcp_tool_id
        )
        assert policy is not None
        policy.risk_class = RiskClass.DESTRUCTIVE.value
        await session.commit()

        outcome = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-worker",
        )
        await session.commit()
        assert outcome.claimed is False
        assert outcome.reason == "PRECONDITION_FAILED"

    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.APPROVED.value
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "APPROVAL_RESUME_PRECONDITION_FAILED"
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []


@pytest.mark.asyncio
async def test_approved_evidence_gate_before_attempt(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without valid APPROVED evidence, READY+requires_approval enters wait (PR #37)."""
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, _ = await _claim_approval_required(
        db_session_factory
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_approved_safe_retry_same_context(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #36 + approval: one APPROVED covers two Attempts of exact same context."""
    from app.agent.plan_validator import PlanValidatorService
    from app.domain.enums import StepAttemptStatus, ToolCallNormalizedStatus
    from app.execution.claim import ExecutionClaimService
    from app.execution.queue import ExecutionQueueService
    from app.repositories.agent_request import AgentRequestRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
    from tests.unit.test_execution_creation import _create, _idem_key
    from tests.unit.test_plan_validator import _seed_validating
    from tests.unit.test_safe_transient_retry import (
        _CONNECT_ERR,
        _FailThenSucceedClient,
    )

    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Retry Gate",
            decision_mode="ANY",
            required_approvals=1,
            allow_self_approval=True,
            approver_scope={},
        )
        await session.commit()
        seeded = await _seed_validating(
            session,
            policy_requires_approval=True,
            approval_policy_id=approval.id,
        )
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.risk_class = RiskClass.READ_ONLY.value
        policy.max_attempts = 2
        await session.commit()
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "READY"
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        seeded = {**seeded, "requester_id": request.requester_id}
        created = await _create(session, seeded, idempotency_key=_idem_key())
        await session.commit()
        execution_id = created.result.id
        requester_id = request.requester_id
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="apr-retry"
        )
        assert claim.claimed and claim.lease_token is not None
        await session.commit()
        lease_token = claim.lease_token

    wait_runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    wait_out = await wait_runner.run_claimed_execution(
        execution_id=execution_id, worker_id="apr-retry", lease_token=lease_token
    )
    assert wait_out.terminal_status == StepStatus.WAITING_APPROVAL.value

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=pending.id,
            actor_user_id=requester_id,
            decision="APPROVE",
        )
        await session.commit()
        resume = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=pending.id,
            worker_id="apr-retry",
        )
        await session.commit()
        assert resume.claimed and resume.lease_token is not None
        token = resume.lease_token
        approval_id = pending.id

    client = _FailThenSucceedClient(first_error=_CONNECT_ERR)
    outcome = await McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    ).run_claimed_execution(
        execution_id=execution_id, worker_id="apr-retry", lease_token=token
    )
    assert client.calls == 2
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    async with db_session_factory() as session:
        approved = await ApprovalRequestRepository(session).get(approval_id)
        assert approved is not None
        assert approved.status == ApprovalStatus.APPROVED.value
        all_for_step = await ApprovalRequestRepository(session).find_approved_for_step(
            execution_id=execution_id,
            step_execution_id=(
                await ExecutionRepository(session).list_steps(execution_id)
            )[0].id,
        )
        assert len(all_for_step) == 1
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 2
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 2
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[1].status == StepAttemptStatus.SUCCEEDED.value
        tc1 = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        tc2 = await ExecutionRepository(session).list_tool_calls(attempts[1].id)
        assert tc1[0].normalized_status == ToolCallNormalizedStatus.FAILED.value
        assert tc2[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
