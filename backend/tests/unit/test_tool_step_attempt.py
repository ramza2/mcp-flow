"""Unit regressions for TOOL Step Attempt foundation."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    BindingKind,
    ExecutionStatus,
    MCPToolStatus,
    ParameterProvenance,
    StepAttemptStatus,
    StepStatus,
    ToolVersionValidationStatus,
)
from app.execution.claim import ExecutionClaimService, _as_utc
from app.execution.queue import ExecutionQueueService
from app.execution.runtime_preflight import assert_current_tool_executable
from app.execution.tool_step_attempt import (
    ToolStepAttemptService,
    materialize_secret_safe_resolved_input,
)
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.user import UserRepository
from app.schemas.parameter_binding import LiteralBindingValue, SecretRefBindingValue
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_creation import (
    _create,
    _idem_key,
    _install_no_side_effects,
    _seed_confirmed_ready,
    _seed_ready,
)
from tests.unit.test_execution_queue_claim import _created_execution


async def _claim_ready(
    session: AsyncSession, *, worker_id: str = "worker-a"
) -> dict[str, Any]:
    execution_id, _ = await _created_execution(session)
    staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
    assert staged == 1
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id=worker_id
    )
    assert claim.claimed is True
    assert claim.lease_token is not None
    assert len(claim.ready_step_ids) == 1
    await session.commit()
    return {
        "execution_id": execution_id,
        "step_id": claim.ready_step_ids[0],
        "worker_id": claim.worker_id,
        "lease_token": claim.lease_token,
        "lease_expires_at": claim.lease_expires_at,
    }


@pytest.mark.asyncio
async def test_start_attempt_happy_path(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    outcome = await ToolStepAttemptService(db_session).start(
        execution_id=claimed["execution_id"],
        step_execution_id=claimed["step_id"],
        worker_id=claimed["worker_id"],
        lease_token=claimed["lease_token"],
    )
    await db_session.commit()

    assert outcome.replayed is False
    assert outcome.attempt_no == 1
    assert outcome.step_status == StepStatus.RUNNING.value
    assert outcome.attempt_status == StepAttemptStatus.STARTED.value

    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING.value
    assert execution.started_at is not None
    started_at = execution.started_at

    steps = await ExecutionRepository(db_session).list_steps(claimed["execution_id"])
    assert len(steps) == 1
    step = steps[0]
    assert step.status == StepStatus.RUNNING.value
    assert step.started_at is not None
    assert step.attempt_count == 1
    assert step.resolved_input is not None

    attempts = await ExecutionRepository(db_session).list_attempts(step.id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.attempt_no == 1
    assert attempt.status == StepAttemptStatus.STARTED.value
    assert attempt.worker_id == claimed["worker_id"]
    # SQLite DateTime(timezone=True) may strip tzinfo; compare via UTC helper.
    assert _as_utc(attempt.lease_expires_at) == _as_utc(claimed["lease_expires_at"])
    assert attempt.finished_at is None

    # Execution.started_at must not be overwritten by attempt start.
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    assert execution.started_at == started_at


@pytest.mark.asyncio
async def test_duplicate_start_is_idempotent(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    first = await ToolStepAttemptService(db_session).start(
        execution_id=claimed["execution_id"],
        step_execution_id=claimed["step_id"],
        worker_id=claimed["worker_id"],
        lease_token=claimed["lease_token"],
    )
    await db_session.commit()
    step_before = (
        await ExecutionRepository(db_session).list_steps(claimed["execution_id"])
    )[0]
    started_at = step_before.started_at
    attempt_count = step_before.attempt_count

    second = await ToolStepAttemptService(db_session).start(
        execution_id=claimed["execution_id"],
        step_execution_id=claimed["step_id"],
        worker_id=claimed["worker_id"],
        lease_token=claimed["lease_token"],
    )
    await db_session.commit()

    assert second.replayed is True
    assert second.attempt_id == first.attempt_id
    assert second.attempt_no == 1
    steps = await ExecutionRepository(db_session).list_steps(claimed["execution_id"])
    assert steps[0].attempt_count == attempt_count == 1
    assert steps[0].started_at == started_at
    attempts = await ExecutionRepository(db_session).list_attempts(claimed["step_id"])
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_wrong_worker_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id="other-worker",
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.READY.value
    assert (
        await ExecutionRepository(db_session).list_attempts(claimed["step_id"])
    ) == []


@pytest.mark.asyncio
async def test_wrong_lease_token_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=uuid.uuid4(),
        )
    assert exc.value.status_code == 409
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.READY.value
    assert (
        await ExecutionRepository(db_session).list_attempts(claimed["step_id"])
    ) == []


@pytest.mark.asyncio
async def test_expired_lease_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert "expired" in exc.value.message.lower()
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.READY.value
    assert (
        await ExecutionRepository(db_session).list_attempts(claimed["step_id"])
    ) == []


@pytest.mark.asyncio
async def test_invalid_step_state_pending_rejected(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    # Manually forge RUNNING lease without READY step.
    execution.status = ExecutionStatus.RUNNING.value
    execution.worker_id = "worker-a"
    execution.lease_token = uuid.uuid4()
    execution.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
    execution.heartbeat_at = datetime.now(UTC)
    execution.started_at = datetime.now(UTC)
    await db_session.commit()
    step = (await ExecutionRepository(db_session).list_steps(execution_id))[0]
    assert step.status == StepStatus.PENDING.value

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=execution_id,
            step_execution_id=step.id,
            worker_id="worker-a",
            lease_token=execution.lease_token,
        )
    assert exc.value.status_code == 409
    assert "READY" in exc.value.message
    assert (await ExecutionRepository(db_session).list_attempts(step.id)) == []


@pytest.mark.asyncio
async def test_non_tool_step_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    step.step_type = "CONDITION"
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert (await ExecutionRepository(db_session).list_attempts(claimed["step_id"])) == []


@pytest.mark.asyncio
async def test_runtime_auth_tool_inactive_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.mcp_tool_version_id is not None
    version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert version is not None
    tool = await MCPToolRepository(db_session).get(version.mcp_tool_id)
    assert tool is not None
    tool.status = MCPToolStatus.INACTIVE.value
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert (await ExecutionRepository(db_session).list_attempts(claimed["step_id"])) == []
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.READY.value


@pytest.mark.asyncio
async def test_runtime_policy_drift_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.mcp_tool_version_id is not None
    version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert version is not None
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(version.mcp_tool_id)
    assert policy is not None
    policy.max_attempts = policy.max_attempts + 1
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert "policy snapshot" in exc.value.message.lower()
    assert (await ExecutionRepository(db_session).list_attempts(claimed["step_id"])) == []


@pytest.mark.asyncio
async def test_secret_ref_stays_reference_only(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    secret_id = uuid.uuid4()
    schema = {
        "type": "object",
        "properties": {"credential": {"type": "string"}},
        "required": ["credential"],
    }
    seeded = await _seed_ready(
        db_session,
        input_schema=schema,
        entities=[
            {
                "name": "credential",
                "value": str(secret_id),
                "source": ParameterProvenance.SECRET_REFERENCE.value,
            }
        ],
    )
    created = await _create(db_session, seeded, idempotency_key=_idem_key())
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=created.result.id, worker_id="worker-secret"
    )
    await db_session.commit()
    assert claim.claimed and claim.lease_token is not None

    outcome = await ToolStepAttemptService(db_session).start(
        execution_id=created.result.id,
        step_execution_id=claim.ready_step_ids[0],
        worker_id="worker-secret",
        lease_token=claim.lease_token,
    )
    await db_session.commit()

    step = (await ExecutionRepository(db_session).list_steps(created.result.id))[0]
    assert step.resolved_input == {
        "credential": {
            "kind": BindingKind.SECRET_REF.value,
            "secret_id": str(secret_id),
        }
    }
    attempts = await ExecutionRepository(db_session).list_attempts(step.id)
    assert len(attempts) == 1
    blob = json.dumps(attempts[0].request_snapshot)
    assert str(secret_id) in blob
    assert "password" not in blob.lower()
    assert "plaintext" not in blob.lower()
    assert "sk-" not in blob.lower()
    assert outcome.attempt_no == 1


def test_materialize_rejects_unsupported_binding_kind() -> None:
    # Construct via model_construct bypass isn't needed — use a fake object.
    class _Bad:
        kind = BindingKind.PLAN_INPUT

    with pytest.raises(AppError) as exc:
        materialize_secret_safe_resolved_input({"x": _Bad()})  # type: ignore[arg-type]
    assert exc.value.status_code == 409


def test_materialize_literal_and_secret_ref() -> None:
    secret_id = uuid.uuid4()
    resolved = materialize_secret_safe_resolved_input(
        {
            "city": LiteralBindingValue(value="Seoul"),
            "token": SecretRefBindingValue(secret_id=secret_id),
        }
    )
    assert resolved == {
        "city": "Seoul",
        "token": {"kind": "SECRET_REF", "secret_id": str(secret_id)},
    }


@pytest.mark.asyncio
async def test_tool_version_invalid_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.mcp_tool_version_id is not None
    version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert version is not None
    version.validation_status = ToolVersionValidationStatus.INVALID.value
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert (await ExecutionRepository(db_session).list_attempts(claimed["step_id"])) == []


@pytest.mark.asyncio
async def test_agent_version_missing_rejected(db_session: AsyncSession) -> None:
    claimed = await _claim_ready(db_session)
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    execution.agent_version_id = uuid.uuid4()
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert (await ExecutionRepository(db_session).list_attempts(claimed["step_id"])) == []
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.READY.value


@pytest.mark.asyncio
async def test_requires_approval_fail_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    approval = await ApprovalPolicyRepository(db_session).create(
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="Attempt Approval Gate",
    )
    await db_session.flush()
    seeded = await _seed_ready(
        db_session,
        policy_requires_approval=True,
        approval_policy_id=approval.id,
    )
    created = await _create(db_session, seeded, idempotency_key=_idem_key())
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=created.result.id, worker_id="worker-approval"
    )
    await db_session.commit()
    assert claim.claimed and claim.lease_token is not None

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=created.result.id,
            step_execution_id=claim.ready_step_ids[0],
            worker_id="worker-approval",
            lease_token=claim.lease_token,
        )
    assert exc.value.status_code == 409
    assert "requires_approval" in exc.value.message.lower()
    step = (await ExecutionRepository(db_session).list_steps(created.result.id))[0]
    assert step.status == StepStatus.READY.value
    assert step.attempt_count == 0
    assert (await ExecutionRepository(db_session).list_attempts(step.id)) == []
    execution = await ExecutionRepository(db_session).get(created.result.id)
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING.value


@pytest.mark.asyncio
async def test_grant_confirmation_drift_without_evidence_rejected(
    db_session: AsyncSession,
) -> None:
    claimed = await _claim_ready(db_session)
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None and execution.agent_version_id is not None
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.mcp_tool_version_id is not None
    version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert version is not None
    grants = await AgentToolGrantRepository(db_session).list_for_version(
        execution.agent_version_id
    )
    grant = next(g for g in grants if g.mcp_tool_id == version.mcp_tool_id)
    assert grant.requires_confirmation is False
    grant.requires_confirmation = True
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert "confirmation" in exc.value.message.lower()
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.READY.value
    assert step.attempt_count == 0
    assert (await ExecutionRepository(db_session).list_attempts(step.id)) == []


@pytest.mark.asyncio
async def test_confirmation_evidence_allows_attempt_start(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_confirmed_ready(db_session)
    created = await _create(db_session, seeded, idempotency_key=_idem_key())
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=created.result.id, worker_id="worker-confirm"
    )
    await db_session.commit()
    assert claim.claimed and claim.lease_token is not None

    outcome = await ToolStepAttemptService(db_session).start(
        execution_id=created.result.id,
        step_execution_id=claim.ready_step_ids[0],
        worker_id="worker-confirm",
        lease_token=claim.lease_token,
    )
    await db_session.commit()
    assert outcome.replayed is False
    assert outcome.attempt_no == 1
    step = (await ExecutionRepository(db_session).list_steps(created.result.id))[0]
    assert step.status == StepStatus.RUNNING.value


@pytest.mark.asyncio
async def test_tool_policy_confirmation_drift_still_fail_closed(
    db_session: AsyncSession,
) -> None:
    claimed = await _claim_ready(db_session)
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.mcp_tool_version_id is not None
    version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert version is not None
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(version.mcp_tool_id)
    assert policy is not None
    assert policy.requires_confirmation is False
    policy.requires_confirmation = True
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert "policy snapshot" in exc.value.message.lower()
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.READY.value
    assert (await ExecutionRepository(db_session).list_attempts(step.id)) == []


@pytest.mark.asyncio
async def test_creation_and_attempt_share_current_tool_preflight(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    calls: list[str] = []
    real = assert_current_tool_executable

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append("preflight")
        return await real(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.execution_creation.assert_current_tool_executable", _spy
    )
    monkeypatch.setattr(
        "app.execution.tool_step_attempt.assert_current_tool_executable", _spy
    )

    seeded = await _seed_ready(db_session)
    created = await _create(db_session, seeded, idempotency_key=_idem_key())
    assert calls == ["preflight"]

    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=created.result.id, worker_id="worker-shared"
    )
    await db_session.commit()
    assert claim.claimed and claim.lease_token is not None

    await ToolStepAttemptService(db_session).start(
        execution_id=created.result.id,
        step_execution_id=claim.ready_step_ids[0],
        worker_id="worker-shared",
        lease_token=claim.lease_token,
    )
    assert calls == ["preflight", "preflight"]


@pytest.mark.asyncio
async def test_start_does_not_rollback_caller_transaction(
    db_session: AsyncSession,
) -> None:
    claimed = await _claim_ready(db_session)
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    user = await UserRepository(db_session).get(execution.requester_id)
    assert user is not None
    probe = f"tx-probe-{uuid.uuid4().hex[:8]}"
    user.display_name = probe

    outcome = await ToolStepAttemptService(db_session).start(
        execution_id=claimed["execution_id"],
        step_execution_id=claimed["step_id"],
        worker_id=claimed["worker_id"],
        lease_token=claimed["lease_token"],
    )
    # session.rollback() inside start would revert the probe before commit.
    assert user.display_name == probe
    await db_session.commit()

    assert outcome.replayed is False
    reloaded = await UserRepository(db_session).get(execution.requester_id)
    assert reloaded is not None
    assert reloaded.display_name == probe
    step = (await ExecutionRepository(db_session).list_steps(claimed["execution_id"]))[0]
    assert step.status == StepStatus.RUNNING.value
    assert step.attempt_count == 1
