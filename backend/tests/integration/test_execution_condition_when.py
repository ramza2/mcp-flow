"""PostgreSQL integration tests for CONDITION + Step.when runtime (PR #48)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    JoinPolicy,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.models.execution import ExecutionStep
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _seed_ready


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {
            "tool_version_id": str(uuid.uuid4()),
            "bindings": {
                "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
            },
        },
    }


def _condition(
    sid: str,
    predicate: dict[str, Any],
    *,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.CONDITION.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"predicate": predicate},
    }


def _join(
    sid: str,
    *,
    depends_on: list[str],
    policy: str = JoinPolicy.ALL_COMPLETE.value,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.JOIN.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"policy": policy},
    }


def _when_eq_c(value: bool) -> dict[str, Any]:
    return {
        "op": "eq",
        "left": {
            "kind": "STEP_OUTPUT",
            "step_id": "c",
            "path": "/condition_result",
        },
        "right": {"kind": "LITERAL", "value": value},
    }


def _branch_specs() -> list[dict[str, Any]]:
    return [
        _tool("a"),
        _condition(
            "c",
            {
                "op": "gte",
                "left": {
                    "kind": "STEP_OUTPUT",
                    "step_id": "a",
                    "path": "/structured_content/score",
                },
                "right": {"kind": "LITERAL", "value": 70},
            },
            depends_on=["a"],
        ),
        _tool("pass", depends_on=["c"], when=_when_eq_c(True)),
        _tool("fail", depends_on=["c"], when=_when_eq_c(False)),
        _join("j", depends_on=["pass", "fail"], policy=JoinPolicy.ALL_COMPLETE.value),
    ]


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
        "goal": "pg condition/when",
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
    def __init__(self, *, score: int) -> None:
        self.calls: list[dict] = []
        self.score = score
        self._lock = asyncio.Lock()

    async def call_tool(self, endpoint, **kwargs):
        async with self._lock:
            self.calls.append({"tool_name": kwargs.get("tool_name")})
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True, "score": self.score},
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


async def _materialize_claim(
    session: AsyncSession,
    *,
    step_specs: list[dict[str, Any]],
    worker_id: str = "pg-cond",
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_true_branch_e2e(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session, step_specs=_branch_specs()
        )
    client = _ScoreClient(score=80)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="pg-cond", lease_token=lease
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["c"].status == StepStatus.SUCCEEDED.value
        assert by["c"].condition_result is True
        assert by["c"].result_inline == {"condition_result": True}
        assert by["c"].attempt_count == 0
        assert await ExecutionRepository(session).list_attempts(by["c"].id) == []
        assert by["pass"].status == StepStatus.SUCCEEDED.value
        assert by["fail"].status == StepStatus.SKIPPED.value
        assert by["fail"].error_code == "STEP_WHEN_FALSE"
        assert by["j"].status == StepStatus.SUCCEEDED.value
        assert len(client.calls) == 2  # a + pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_false_branch_e2e(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session, step_specs=_branch_specs(), worker_id="pg-cond-f"
        )
    client = _ScoreClient(score=10)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="pg-cond-f", lease_token=lease
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["c"].condition_result is False
        assert by["c"].result_inline == {"condition_result": False}
        assert by["pass"].status == StepStatus.SKIPPED.value
        assert by["fail"].status == StepStatus.SUCCEEDED.value
        assert by["j"].status == StepStatus.SUCCEEDED.value
        assert len(client.calls) == 2  # a + fail


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_join_skipped_branch_policies(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ALL_COMPLETE with skipped branch → SUCCEEDED
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=_branch_specs(),
            worker_id="pg-join-ac",
        )
    client = _ScoreClient(score=80)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="pg-join-ac", lease_token=lease
    )
    async with integration_session_factory() as session:
        by = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert by["j"].status == StepStatus.SUCCEEDED.value

    # ANY_SUCCESS with one SUCCEEDED + one SKIPPED → SUCCEEDED
    specs = [
        _tool("a"),
        _condition(
            "c",
            {
                "op": "eq",
                "left": {"kind": "LITERAL", "value": True},
                "right": {"kind": "LITERAL", "value": True},
            },
            depends_on=["a"],
        ),
        _tool("pass", depends_on=["c"], when=_when_eq_c(True)),
        _tool("fail", depends_on=["c"], when=_when_eq_c(False)),
        _join("j", depends_on=["pass", "fail"], policy=JoinPolicy.ANY_SUCCESS.value),
    ]
    async with integration_session_factory() as session:
        execution_id2, lease2, _ = await _materialize_claim(
            session, step_specs=specs, worker_id="pg-join-any"
        )
    client2 = _ScoreClient(score=1)
    runner2 = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client2,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    await runner2.run_claimed_execution(
        execution_id=execution_id2, worker_id="pg-join-any", lease_token=lease2
    )
    async with integration_session_factory() as session:
        by2 = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id2)
        }
        assert by2["j"].status == StepStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_duplicate_condition_reconcile_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session, step_specs=_branch_specs(), worker_id="pg-race"
        )
    client = _ScoreClient(score=80)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )

    async def _run() -> Any:
        return await runner.run_claimed_execution(
            execution_id=execution_id,
            worker_id="pg-race",
            lease_token=lease,
        )

    results = await asyncio.gather(_run(), _run(), return_exceptions=True)
    assert not any(isinstance(r, BaseException) for r in results)
    async with integration_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["c"].status == StepStatus.SUCCEEDED.value
        assert by["c"].condition_result is True
        assert by["pass"].status == StepStatus.SUCCEEDED.value
        assert by["fail"].status == StepStatus.SKIPPED.value
        assert len(client.calls) <= 2
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_post_claim_predicate_tamper_fail_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        execution_id, lease, _ = await _materialize_claim(
            session,
            step_specs=[
                _condition(
                    "c",
                    {
                        "op": "eq",
                        "left": {"kind": "LITERAL", "value": True},
                        "right": {"kind": "LITERAL", "value": True},
                    },
                ),
                _tool("t", depends_on=["c"]),
            ],
            worker_id="pg-tamper",
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        c = next(s for s in steps if s.step_key == "c")
        snap = dict(c.step_snapshot)
        snap["config"] = {
            "predicate": {
                "op": "eq",
                "left": {"kind": "LITERAL", "value": False},
                "right": {"kind": "LITERAL", "value": True},
            }
        }
        await session.execute(
            update(ExecutionStep)
            .where(ExecutionStep.id == c.id)
            .values(step_snapshot=snap, lock_version=c.lock_version + 1)
        )
        await session.commit()

    client = _ScoreClient(score=80)
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id="pg-tamper", lease_token=lease
    )
    assert outcome.reason == "EXECUTION_FAILED"
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        assert execution.worker_id is None
        assert len(client.calls) == 0
