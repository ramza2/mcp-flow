"""Unit tests for authorable APPROVAL Step runtime (PR #49)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.approval.context import APPROVAL_CONTEXT_SCHEMA_VERSION
from app.approval.decision import ApprovalDecisionService
from app.approval.evidence import validate_stored_context_snapshot
from app.approval.expiry import ApprovalExpiryService
from app.approval.query import ApprovalQueryService, project_safe_context
from app.approval.step_context import (
    APPROVAL_KIND_AUTHORABLE_STEP,
    APPROVAL_STEP_CONTEXT_SCHEMA_VERSION,
    build_approval_step_context_snapshot,
    compute_approval_step_context_hash,
    hash_result_inline,
)
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalStatus,
    AuthorableStepType,
    ExecutionStatus,
    StepStatus,
)
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.tool_runner import McpToolRunner
from app.models.approval import ApprovalRequest
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import ExecutionPlanV1
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_approval_decision_resume import _grant_decide
from tests.unit.test_execution_condition_when import (
    _ScoreClient,
    _claim_and_run,
    _condition_step,
    _plan,
    _resolver_factory,
    _seed_executable,
    _tool_step,
)
from tests.unit.test_execution_creation import _install_no_side_effects


def _approval_step(
    sid: str,
    approval_policy_id: uuid.UUID,
    *,
    depends_on: list[str] | None = None,
    when: dict[str, Any] | None = None,
    on_error: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.APPROVAL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": 30,
        "on_error": on_error,
        "config": {"approval_policy_id": str(approval_policy_id)},
    }


async def _create_self_approval_policy(
    session: AsyncSession,
    *,
    approver_scope: dict[str, Any] | None = None,
    default_expiry_seconds: int = 3600,
    on_code: str | None = None,
) -> Any:
    # Prefer {} over None so PG JSONB does not persist JSON null
    # (ck_approval_requests_approval_scope_object requires SQL NULL or object).
    scope = {} if approver_scope is None else approver_scope
    return await ApprovalPolicyRepository(session).create(
        code=on_code or f"ap-auth-{uuid.uuid4().hex[:8]}",
        name="Authorable APPROVAL Gate",
        decision_mode="ANY",
        required_approvals=1,
        default_expiry_seconds=default_expiry_seconds,
        approver_scope=scope,
        allow_self_approval=True,
        reject_comment_required=False,
    )


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


def _when_literal(value: bool) -> dict[str, Any]:
    return {
        "op": "eq",
        "left": {"kind": "LITERAL", "value": True},
        "right": {"kind": "LITERAL", "value": value},
    }


async def _enter_authorable_wait(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    steps_factory,
    client: Any | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any], Any]:
    """Materialize + claim + run until WAITING_APPROVAL.

    Returns (execution_id, approval_request_id, requester_id, seeded, client).
    """
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        policy = await _create_self_approval_policy(session)
        await session.flush()
        plan = _plan(steps_factory(policy.id), seeded["tool_version_id"])
        await session.commit()

    mcp = client or _ScoreClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=mcp
    )
    assert outcome.reason == "WAITING_APPROVAL"
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        return execution_id, pending.id, seeded["requester_id"], seeded, mcp


async def _decide_approve(
    session: AsyncSession,
    *,
    approval_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    await ApprovalDecisionService(session).decide(
        approval_id=approval_id,
        actor_user_id=actor_user_id,
        decision="APPROVE",
    )


async def _approve_resume_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    approval_id: uuid.UUID,
    requester_id: uuid.UUID,
    client: Any,
    worker_id: str = "resume-appr",
) -> Any:
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        await _decide_approve(
            session, approval_id=approval_id, actor_user_id=requester_id
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

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    return await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease,
    )


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_approval_enters_wait(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _req, _seeded, client = await _enter_authorable_wait(
        db_session_factory,
        monkeypatch,
        steps_factory=lambda pid: [_approval_step("p", pid)],
    )
    assert len(client.calls) == 0
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.lease_expires_at is None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        step = steps[0]
        assert step.step_key == "p"
        assert step.status == StepStatus.WAITING_APPROVAL.value
        assert await ExecutionRepository(session).list_attempts(step.id) == []
        requests = await _list_requests(session, execution_id)
        assert len(requests) == 1
        assert requests[0].id == approval_id
        assert requests[0].status == ApprovalStatus.PENDING.value


@pytest.mark.asyncio
async def test_dependency_approval_waits_after_barrier(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, _approval_id, _req, _seeded, client = await _enter_authorable_wait(
        db_session_factory,
        monkeypatch,
        steps_factory=lambda pid: [
            _tool_step("a"),
            _approval_step("p", pid, depends_on=["a"]),
        ],
    )
    assert len(client.calls) == 1
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.worker_id is None
        assert execution.lease_token is None
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert set(by) == {"a", "p"}
        assert "b" not in by
        assert by["a"].status == StepStatus.SUCCEEDED.value
        assert by["p"].status == StepStatus.WAITING_APPROVAL.value
        assert await ExecutionRepository(session).list_attempts(by["p"].id) == []


@pytest.mark.asyncio
async def test_when_false_approval_skips_no_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        policy = await _create_self_approval_policy(session)
        await session.flush()
        plan = _plan(
            [
                _approval_step(
                    "p",
                    policy.id,
                    when=_when_literal(False),
                )
            ],
            seeded["tool_version_id"],
        )
        await session.commit()

    client = _ScoreClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    async with db_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p"].status == StepStatus.SKIPPED.value
        assert by["p"].error_code == "STEP_WHEN_FALSE"
        assert await _list_requests(session, execution_id) == []


@pytest.mark.asyncio
async def test_upstream_conditional_skip_prunes_approval(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        policy = await _create_self_approval_policy(session)
        await session.flush()
        plan = _plan(
            [
                _condition_step(
                    "c",
                    {
                        "op": "eq",
                        "left": {"kind": "LITERAL", "value": True},
                        "right": {"kind": "LITERAL", "value": True},
                    },
                    when=_when_literal(False),
                ),
                _approval_step("p", policy.id, depends_on=["c"]),
            ],
            seeded["tool_version_id"],
        )
        await session.commit()

    client = _ScoreClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    async with db_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["c"].error_code == "STEP_WHEN_FALSE"
        assert by["p"].status == StepStatus.SKIPPED.value
        assert by["p"].error_code == "UPSTREAM_CONDITION_SKIPPED"
        assert await _list_requests(session, execution_id) == []


@pytest.mark.asyncio
async def test_approved_ready_approval_succeeds_canonical_result(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester_id, _seeded, client = (
        await _enter_authorable_wait(
            db_session_factory,
            monkeypatch,
            steps_factory=lambda pid: [
                _tool_step("a"),
                _approval_step("p", pid, depends_on=["a"]),
                _tool_step("b", depends_on=["p"]),
            ],
        )
    )
    assert len(client.calls) == 1  # A only so far

    outcome = await _approve_resume_run(
        db_session_factory,
        execution_id=execution_id,
        approval_id=approval_id,
        requester_id=requester_id,
        client=client,
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 2  # A + B

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["a"].status == StepStatus.SUCCEEDED.value
        assert by["p"].status == StepStatus.SUCCEEDED.value
        assert by["p"].result_inline == {
            "approval_status": ApprovalStatus.APPROVED.value,
            "approval_request_id": str(approval_id),
        }
        assert await ExecutionRepository(session).list_attempts(by["p"].id) == []
        assert by["b"].status == StepStatus.SUCCEEDED.value
        attempts_b = await ExecutionRepository(session).list_attempts(by["b"].id)
        assert len(attempts_b) == 1
        tcs = await ExecutionRepository(session).list_tool_calls(attempts_b[0].id)
        assert len(tcs) == 1


@pytest.mark.asyncio
async def test_two_eligible_approvals_serialize_plan_order(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        p1_pol = await _create_self_approval_policy(session)
        p2_pol = await _create_self_approval_policy(session)
        await session.flush()
        plan = _plan(
            [
                _approval_step("p1", p1_pol.id),
                _approval_step("p2", p2_pol.id),
            ],
            seeded["tool_version_id"],
        )
        await session.commit()

    client = _ScoreClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert outcome.reason == "WAITING_APPROVAL"
    async with db_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p1"].status == StepStatus.WAITING_APPROVAL.value
        assert by["p2"].status == StepStatus.PENDING.value
        requests = await _list_requests(session, execution_id)
        assert len(requests) == 1
        assert requests[0].status == ApprovalStatus.PENDING.value
        assert requests[0].step_execution_id == by["p1"].id


@pytest.mark.asyncio
async def test_rejection_ignores_continue(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester_id, _seeded, client = (
        await _enter_authorable_wait(
            db_session_factory,
            monkeypatch,
            steps_factory=lambda pid: [
                _approval_step("p", pid, on_error="CONTINUE"),
                _tool_step("b", depends_on=["p"]),
            ],
        )
    )
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=requester_id,
            decision="REJECT",
            comment="nope",
        )
        await session.commit()

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "APPROVAL_REJECTED"
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p"].status == StepStatus.FAILED.value
        assert by["p"].error_code == "APPROVAL_REJECTED"
        assert by["b"].status == StepStatus.SKIPPED.value
        assert len(client.calls) == 0
        assert await ExecutionRepository(session).list_attempts(by["b"].id) == []


@pytest.mark.asyncio
async def test_expiry_ignores_mark_partial(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _req, _seeded, _client = await _enter_authorable_wait(
        db_session_factory,
        monkeypatch,
        steps_factory=lambda pid: [
            _approval_step("p", pid, on_error="MARK_PARTIAL"),
            _tool_step("b", depends_on=["p"]),
        ],
    )
    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        request.expires_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()
        result = await ApprovalExpiryService(session).expire_due_batch(limit=10)
        await session.commit()
        assert result.expired >= 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "APPROVAL_EXPIRED"
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["p"].status == StepStatus.FAILED.value
        assert by["p"].error_code == "APPROVAL_EXPIRED"
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        assert request.status == ApprovalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_toolpolicy_multistep_still_unsupported(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ToolPolicy multi-Step DAG wait remains fail-closed (DAG_WAIT_UNSUPPORTED)."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-dag-{uuid.uuid4().hex[:8]}",
            name="DAG Wait Unsupported Gate",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={"role_codes": ["ops"]},
        )
        await session.flush()
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.requires_approval = True
        policy.approval_policy_id = approval.id
        seeded["policy_snapshot"] = build_safe_tool_policy_snapshot(policy, approval)
        plan = _plan(
            [_tool_step("a"), _tool_step("b")],
            seeded["tool_version_id"],
            max_parallelism=2,
        )
        await session.commit()

    client = _ScoreClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    assert len(client.calls) == 0
    assert outcome.reason in {"DAG_WAIT_UNSUPPORTED", "EXECUTION_FAILED"}
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.status != ExecutionStatus.WAITING_APPROVAL.value


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorable_context_deterministic_and_upstream_order(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _req, _seeded, _client = await _enter_authorable_wait(
        db_session_factory,
        monkeypatch,
        steps_factory=lambda pid: [
            _tool_step("a"),
            _tool_step("c", depends_on=["a"]),
            _approval_step("p", pid, depends_on=["a", "c"]),
        ],
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        policy = await ApprovalPolicyRepository(session).get(request.approval_policy_id)
        assert policy is not None
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)

        snap1 = build_approval_step_context_snapshot(
            execution=execution,
            step=by["p"],
            plan=plan,
            approval_policy=policy,
            steps=steps,
        )
        snap2 = build_approval_step_context_snapshot(
            execution=execution,
            step=by["p"],
            plan=plan,
            approval_policy=policy,
            steps=steps,
        )
        assert snap1 == snap2
        assert snap1 == request.context_snapshot
        assert snap1["schema_version"] == APPROVAL_STEP_CONTEXT_SCHEMA_VERSION
        assert snap1["approval_kind"] == APPROVAL_KIND_AUTHORABLE_STEP
        upstream = snap1["upstream_evidence"]
        assert [u["step_key"] for u in upstream] == ["a", "c"]
        for item in upstream:
            assert "result_inline_hash" in item
            assert "result_inline" not in item
            assert item["result_inline_hash"] == hash_result_inline(
                by[item["step_key"]].result_inline
            )
        # Raw TOOL result must not appear in context snapshot.
        blob = str(snap1)
        assert "structured_content" not in blob
        assert by["a"].result_inline is not None
        assert "score" not in blob or hash_result_inline(by["a"].result_inline) in blob


@pytest.mark.asyncio
async def test_context_hash_detects_upstream_result_mutation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, _req, _seeded, _client = await _enter_authorable_wait(
        db_session_factory,
        monkeypatch,
        steps_factory=lambda pid: [
            _tool_step("a"),
            _approval_step("p", pid, depends_on=["a"]),
        ],
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        policy = await ApprovalPolicyRepository(session).get(request.approval_policy_id)
        assert policy is not None
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        original = compute_approval_step_context_hash(request.context_snapshot)

        by["a"].result_inline = {"ok": True, "score": 999, "mutated": True}
        await session.flush()
        steps2 = await ExecutionRepository(session).list_steps(execution_id)
        rebuilt = build_approval_step_context_snapshot(
            execution=execution,
            step=by["p"],
            plan=plan,
            approval_policy=policy,
            steps=steps2,
        )
        assert compute_approval_step_context_hash(rebuilt) != original


def test_unknown_context_schema_fail_closed() -> None:
    with pytest.raises(AppError) as exc:
        validate_stored_context_snapshot(
            {"schema_version": "garbage.v9", "approval_kind": "X"}
        )
    assert exc.value.status_code == 409


def test_tool_context_validation_unchanged() -> None:
    snap = {
        "schema_version": APPROVAL_CONTEXT_SCHEMA_VERSION,
        "execution_id": str(uuid.uuid4()),
        "step_execution_id": str(uuid.uuid4()),
        "step_key": "tool-1",
        "requester_id": str(uuid.uuid4()),
        "agent_version_id": str(uuid.uuid4()),
        "mcp_tool_version_id": str(uuid.uuid4()),
        "plan_hash": "a" * 64,
        "risk_class": "READ_ONLY",
        "resolved_input": {"q": "hi"},
        "tool_policy": {
            "id": str(uuid.uuid4()),
            "requires_approval": True,
            "requires_confirmation": False,
            "approval_policy_id": str(uuid.uuid4()),
            "risk_class": "READ_ONLY",
        },
        "approval_policy": {
            "id": str(uuid.uuid4()),
            "status": "ACTIVE",
            "decision_mode": "ANY",
            "required_approvals": 1,
            "approver_scope": None,
            "default_expiry_seconds": 3600,
            "allow_self_approval": False,
            "reject_comment_required": False,
        },
    }
    validated = validate_stored_context_snapshot(snap)
    assert validated["schema_version"] == APPROVAL_CONTEXT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorable_resume_fresh_lease_ready(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester_id, _seeded, _client = (
        await _enter_authorable_wait(
            db_session_factory,
            monkeypatch,
            steps_factory=lambda pid: [_approval_step("p", pid)],
        )
    )
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        await _decide_approve(
            session, approval_id=approval_id, actor_user_id=requester_id
        )
        await session.commit()
        claim = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-fresh",
        )
        await session.commit()
        assert claim.claimed is True
        assert claim.lease_token is not None
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.RUNNING.value
        assert execution.worker_id == "resume-fresh"
        assert execution.lease_token == claim.lease_token
        assert execution.lease_expires_at is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.READY.value


@pytest.mark.asyncio
async def test_authorable_resume_policy_drift_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester_id, _seeded, _client = (
        await _enter_authorable_wait(
            db_session_factory,
            monkeypatch,
            steps_factory=lambda pid: [_approval_step("p", pid)],
        )
    )
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        await _decide_approve(
            session, approval_id=approval_id, actor_user_id=requester_id
        )
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        policy = await ApprovalPolicyRepository(session).get(request.approval_policy_id)
        assert policy is not None
        policy.decision_mode = "ALL"
        policy.required_approvals = 2
        await session.commit()

        claim = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-drift",
        )
        await session.commit()
        assert claim.claimed is False
        assert claim.reason == "PRECONDITION_FAILED"

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "APPROVAL_RESUME_PRECONDITION_FAILED"
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value


@pytest.mark.asyncio
async def test_duplicate_resume_stale_delivery(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester_id, _seeded, _client = (
        await _enter_authorable_wait(
            db_session_factory,
            monkeypatch,
            steps_factory=lambda pid: [_approval_step("p", pid)],
        )
    )
    async with db_session_factory() as session:
        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        await _decide_approve(
            session, approval_id=approval_id, actor_user_id=requester_id
        )
        await session.commit()
        first = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-1",
        )
        await session.commit()
        assert first.claimed is True
        first_token = first.lease_token

        dup = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="resume-2",
        )
        await session.commit()
        assert dup.claimed is False
        assert dup.reason == "STALE_DELIVERY"
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.lease_token == first_token
        assert execution.worker_id == "resume-1"


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorable_safe_projection(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, approval_id, requester_id, _seeded, _client = (
        await _enter_authorable_wait(
            db_session_factory,
            monkeypatch,
            steps_factory=lambda pid: [
                _tool_step("a"),
                _approval_step("p", pid, depends_on=["a"]),
            ],
        )
    )
    async with db_session_factory() as session:
        request = await ApprovalRequestRepository(session).get(approval_id)
        assert request is not None
        projected = project_safe_context(request.context_snapshot)
        assert projected["approval_kind"] == APPROVAL_KIND_AUTHORABLE_STEP
        assert projected["step_key"] == "p"
        assert "upstream_steps" in projected
        assert projected["upstream_steps"][0]["step_key"] == "a"
        assert "context_hash" not in projected
        assert "context_snapshot" not in projected
        blob = str(projected)
        assert "result_inline" not in blob
        assert "score" not in blob

        await _grant_decide(session, user_id=requester_id)
        await session.commit()
        detail = await ApprovalQueryService(session).get_for_actor(
            actor_user_id=requester_id,
            approval_id=approval_id,
        )
        assert detail.safe_context["approval_kind"] == APPROVAL_KIND_AUTHORABLE_STEP
        assert detail.safe_context["step_key"] == "p"
        assert "context_hash" not in detail.safe_context
        assert detail.item.execution_id == execution_id
