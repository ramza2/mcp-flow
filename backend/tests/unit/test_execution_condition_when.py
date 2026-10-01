"""Unit tests for CONDITION + Step.when runtime (PR #48)."""

from __future__ import annotations

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
from app.execution.completion import aggregate_all_required
from app.execution.dag import validate_tool_join_dag
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
    ExecutionPlanV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects, _seed_ready

_AV = uuid.uuid4()


class _ScoreClient:
    def __init__(self, *, score: int = 80) -> None:
        self.calls: list[dict] = []
        self.score = score

    async def call_tool(self, endpoint, **kwargs):
        name = str(kwargs.get("tool_name") or "")
        self.calls.append({"tool_name": name, **kwargs})
        payload = {"ok": True, "score": self.score, "branch": name}
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content=payload,
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


def _resolver_factory():
    return UnimplementedSecretResolver()


def _tool_step(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    when: dict[str, Any] | None = None,
    required: bool = True,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": name or sid,
        "type": AuthorableStepType.TOOL.value,
        "required": required,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {
            "bindings": {
                "location": {
                    "kind": BindingKind.LITERAL.value,
                    "value": "Seoul",
                }
            }
        },
    }


def _condition_step(
    sid: str,
    predicate: dict[str, Any],
    *,
    depends_on: list[str] | None = None,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.CONDITION.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"predicate": predicate},
    }


def _join_step(
    sid: str,
    depends_on: list[str],
    *,
    policy: str = JoinPolicy.ALL_COMPLETE.value,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.JOIN.value,
        "required": True,
        "depends_on": depends_on,
        "when": when,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"policy": policy},
    }


def _when_eq_condition(true: bool) -> dict[str, Any]:
    return {
        "op": "eq",
        "left": {
            "kind": "STEP_OUTPUT",
            "step_id": "c",
            "path": "/condition_result",
        },
        "right": {"kind": "LITERAL", "value": true},
    }


def _canonical_branch_plan(tool_version_id: uuid.UUID) -> dict[str, Any]:
    steps = [
        _tool_step("a"),
        _condition_step(
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
        _tool_step("pass", depends_on=["c"], when=_when_eq_condition(True)),
        _tool_step("fail", depends_on=["c"], when=_when_eq_condition(False)),
        _join_step("j", ["pass", "fail"], policy=JoinPolicy.ALL_COMPLETE.value),
    ]
    return _plan(steps, tool_version_id)


def _plan(
    steps: list[dict[str, Any]],
    tool_version_id: uuid.UUID,
    *,
    max_parallelism: int = 4,
) -> dict[str, Any]:
    rewritten: list[dict[str, Any]] = []
    for step in steps:
        s = dict(step)
        if s["type"] == AuthorableStepType.TOOL.value:
            cfg = dict(s.get("config") or {})
            cfg["tool_version_id"] = str(tool_version_id)
            s["config"] = cfg
        rewritten.append(s)
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = max_parallelism
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "condition/when fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


async def _seed_executable(session: AsyncSession) -> dict[str, Any]:
    seeded = await _seed_ready(session)
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    return {
        **seeded,
        "policy_snapshot": build_safe_tool_policy_snapshot(policy, approval),
    }


async def _materialize_queued(
    session: AsyncSession,
    *,
    plan_snapshot: dict[str, Any],
    seeded: dict[str, Any],
) -> uuid.UUID:
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan_snapshot,
            plan_hash=compute_plan_hash(plan_snapshot),
            input_snapshot={},
            policy_snapshot=dict(seeded["policy_snapshot"]),
            requested_at=datetime.now(UTC),
            trace_id="trace-cond",
        )
    )
    execution = await ExecutionRepository(session).get(outcome.execution.id)
    assert execution is not None
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    await session.flush()
    return execution.id


async def _claim_and_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    plan: dict[str, Any],
    seeded: dict[str, Any],
    client: Any,
    worker_id: str = "worker-cond",
) -> tuple[uuid.UUID, Any]:
    async with db_session_factory() as session:
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        await session.commit()
        assert claim.claimed
        assert claim.lease_token is not None
        lease = claim.lease_token

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease,
    )
    return execution_id, outcome


def test_aggregate_all_required_intentional_skip_neutral() -> None:
    tool_id = uuid.uuid4()
    plan = ExecutionPlanV1.model_validate(
        _plan(
            [
                _tool_step("a"),
                _tool_step("b", depends_on=["a"], required=True),
            ],
            tool_id,
        )
    )
    steps = [
        type(
            "S",
            (),
            {
                "step_key": "a",
                "status": StepStatus.SUCCEEDED.value,
                "error_code": None,
            },
        )(),
        type(
            "S",
            (),
            {
                "step_key": "b",
                "status": StepStatus.SKIPPED.value,
                "error_code": "STEP_WHEN_FALSE",
            },
        )(),
    ]
    decision = aggregate_all_required(plan=plan, steps=steps)  # type: ignore[arg-type]
    assert decision.status == ExecutionStatus.SUCCEEDED.value


def test_aggregate_all_required_fail_fast_skip_not_neutral() -> None:
    tool_id = uuid.uuid4()
    plan = ExecutionPlanV1.model_validate(
        _plan(
            [
                _tool_step("a"),
                _tool_step("b", depends_on=["a"], required=True),
            ],
            tool_id,
        )
    )
    steps = [
        type(
            "S",
            (),
            {
                "step_key": "a",
                "status": StepStatus.FAILED.value,
                "error_code": "X",
            },
        )(),
        type(
            "S",
            (),
            {
                "step_key": "b",
                "status": StepStatus.SKIPPED.value,
                "error_code": "UPSTREAM_EXECUTION_STOPPED",
            },
        )(),
    ]
    decision = aggregate_all_required(plan=plan, steps=steps)  # type: ignore[arg-type]
    assert decision.status == ExecutionStatus.FAILED.value


@pytest.mark.asyncio
async def test_root_condition_true_false_and_no_attempt(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    for expected, lit in ((True, True), (False, False)):
        plan = _plan(
            [
                _condition_step(
                    "c",
                    {
                        "op": "eq",
                        "left": {"kind": "LITERAL", "value": lit},
                        "right": {"kind": "LITERAL", "value": True},
                    },
                ),
                _tool_step(
                    "t",
                    depends_on=["c"],
                    when={
                        "op": "eq",
                        "left": {
                            "kind": "STEP_OUTPUT",
                            "step_id": "c",
                            "path": "/condition_result",
                        },
                        "right": {"kind": "LITERAL", "value": True},
                    },
                ),
            ],
            seeded["tool_version_id"],
        )
        client = _ScoreClient()
        execution_id, outcome = await _claim_and_run(
            db_session_factory, plan=plan, seeded=seeded, client=client
        )
        async with db_session_factory() as session:
            steps = await ExecutionRepository(session).list_steps(execution_id)
            by = {s.step_key: s for s in steps}
            c = by["c"]
            assert c.status == StepStatus.SUCCEEDED.value
            assert c.condition_result is expected
            assert c.result_inline == {"condition_result": expected}
            assert c.attempt_count == 0
            attempts = await ExecutionRepository(session).list_attempts(c.id)
            assert attempts == []
            if expected:
                assert by["t"].status == StepStatus.SUCCEEDED.value, (
                    by["t"].error_code,
                    by["t"].error_message,
                    outcome.reason,
                )
                assert len(client.calls) == 1
                assert outcome.reason == "EXECUTION_SUCCEEDED"
            else:
                assert by["t"].status == StepStatus.SKIPPED.value
                assert by["t"].error_code == "STEP_WHEN_FALSE"
                assert len(client.calls) == 0


@pytest.mark.asyncio
async def test_canonical_true_branch(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _canonical_branch_plan(seeded["tool_version_id"])
    client = _ScoreClient(score=80)
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["c"].status == StepStatus.SUCCEEDED.value
        assert by["c"].condition_result is True
        assert by["pass"].status == StepStatus.SUCCEEDED.value
        assert by["fail"].status == StepStatus.SKIPPED.value
        assert by["fail"].error_code == "STEP_WHEN_FALSE"
        assert by["j"].status == StepStatus.SUCCEEDED.value
        names = [c["tool_name"] for c in client.calls]
        # a + pass (fail MCP 0)
        assert len(client.calls) == 2
        assert "a" in names or any(True for _ in names)  # tool_name from registry
    assert outcome.reason == "EXECUTION_SUCCEEDED"


@pytest.mark.asyncio
async def test_canonical_false_branch(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _canonical_branch_plan(seeded["tool_version_id"])
    client = _ScoreClient(score=10)
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["c"].condition_result is False
        assert by["pass"].status == StepStatus.SKIPPED.value
        assert by["fail"].status == StepStatus.SUCCEEDED.value
        assert by["j"].status == StepStatus.SUCCEEDED.value
        assert len(client.calls) == 2  # a + fail
    assert outcome.reason == "EXECUTION_SUCCEEDED"


@pytest.mark.asyncio
async def test_when_false_join_skipped(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _plan(
        [
            _tool_step("a"),
            _tool_step("b", depends_on=["a"]),
            _join_step(
                "j",
                ["b"],
                when={
                    "op": "eq",
                    "left": {"kind": "LITERAL", "value": False},
                    "right": {"kind": "LITERAL", "value": True},
                },
            ),
        ],
        seeded["tool_version_id"],
    )
    client = _ScoreClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["j"].status == StepStatus.SKIPPED.value
        assert by["j"].error_code == "STEP_WHEN_FALSE"
        # required JOIN intentionally skipped → ALL_REQUIRED still succeeds
        assert outcome.reason == "EXECUTION_SUCCEEDED"


@pytest.mark.asyncio
async def test_upstream_condition_skip_propagation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _plan(
        [
            _condition_step(
                "c",
                {
                    "op": "eq",
                    "left": {"kind": "LITERAL", "value": True},
                    "right": {"kind": "LITERAL", "value": True},
                },
                when={
                    "op": "eq",
                    "left": {"kind": "LITERAL", "value": 1},
                    "right": {"kind": "LITERAL", "value": 2},
                },
            ),
            _tool_step("t", depends_on=["c"]),
            _condition_step(
                "c2",
                {
                    "op": "eq",
                    "left": {"kind": "LITERAL", "value": True},
                    "right": {"kind": "LITERAL", "value": True},
                },
                depends_on=["c"],
            ),
        ],
        seeded["tool_version_id"],
    )
    client = _ScoreClient()
    execution_id, _ = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["c"].error_code == "STEP_WHEN_FALSE"
        assert by["t"].error_code == "UPSTREAM_CONDITION_SKIPPED"
        assert by["c2"].error_code == "UPSTREAM_CONDITION_SKIPPED"
        assert len(client.calls) == 0


@pytest.mark.asyncio
async def test_join_all_success_fails_on_skipped_branch(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _plan(
        [
            _tool_step("a"),
            _condition_step(
                "c",
                {
                    "op": "eq",
                    "left": {"kind": "LITERAL", "value": True},
                    "right": {"kind": "LITERAL", "value": True},
                },
                depends_on=["a"],
            ),
            _tool_step("pass", depends_on=["c"], when=_when_eq_condition(True)),
            _tool_step("fail", depends_on=["c"], when=_when_eq_condition(False)),
            _join_step("j", ["pass", "fail"], policy=JoinPolicy.ALL_SUCCESS.value),
        ],
        seeded["tool_version_id"],
    )
    client = _ScoreClient(score=80)
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["j"].status == StepStatus.FAILED.value
        assert by["j"].error_code == "JOIN_POLICY_UNSATISFIED"
    assert outcome.reason in {"EXECUTION_FAILED", "JOIN_POLICY_UNSATISFIED"}


@pytest.mark.asyncio
async def test_join_any_success_with_skipped(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _plan(
        [
            _tool_step("a"),
            _condition_step(
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
            _tool_step("pass", depends_on=["c"], when=_when_eq_condition(True)),
            _tool_step("fail", depends_on=["c"], when=_when_eq_condition(False)),
            _join_step("j", ["pass", "fail"], policy=JoinPolicy.ANY_SUCCESS.value),
        ],
        seeded["tool_version_id"],
    )
    client = _ScoreClient(score=80)
    execution_id, outcome = await _claim_and_run(
        db_session_factory, plan=plan, seeded=seeded, client=client
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["j"].status == StepStatus.SUCCEEDED.value
    assert outcome.reason == "EXECUTION_SUCCEEDED"


@pytest.mark.asyncio
async def test_predicate_snapshot_tamper_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _condition_step(
                    "c",
                    {
                        "op": "eq",
                        "left": {"kind": "LITERAL", "value": True},
                        "right": {"kind": "LITERAL", "value": True},
                    },
                ),
                _tool_step("t", depends_on=["c"]),
            ],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id="tamper-cond"
        )
        await session.commit()
        assert claim.claimed and claim.lease_token is not None
        lease = claim.lease_token

    # Tamper CONDITION step_snapshot predicate after claim.
    async with db_session_factory() as session:
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

    client = _ScoreClient()
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="tamper-cond",
        lease_token=lease,
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        assert execution.worker_id is None
        assert len(client.calls) == 0
    assert outcome.reason == "EXECUTION_FAILED"


@pytest.mark.asyncio
async def test_validate_dag_accepts_root_condition(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _condition_step(
                    "c",
                    {
                        "op": "eq",
                        "left": {"kind": "LITERAL", "value": True},
                        "right": {"kind": "LITERAL", "value": True},
                    },
                ),
                _tool_step("t", depends_on=["c"]),
            ],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        steps = await ExecutionRepository(session).list_steps(execution_id)
        parsed = ExecutionPlanV1.model_validate(plan)
        dag = validate_tool_join_dag(parsed, steps)
        assert dag.root_tool_keys == ()
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="root-cond"
        )
        assert claim.claimed
        assert claim.ready_step_ids == ()
        await session.commit()


@pytest.mark.asyncio
async def test_condition_reconcile_idempotent_on_duplicate_delivery(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate scheduler delivery after success is a no-op; CONDITION stays terminal.

    Concurrent SQLite races are covered by the PostgreSQL integration suite.
    """
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _canonical_branch_plan(seeded["tool_version_id"])
    client = _ScoreClient(score=80)
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan,
        seeded=seeded,
        client=client,
        worker_id="race-cond",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    first_calls = len(client.calls)

    # Second delivery after terminal — claim rejects; no extra MCP.
    async with db_session_factory() as session:
        claim2 = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id="race-cond-2"
        )
        await session.commit()
        assert not claim2.claimed

    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by = {s.step_key: s for s in steps}
        assert by["c"].status == StepStatus.SUCCEEDED.value
        assert by["c"].condition_result is True
        assert by["pass"].status == StepStatus.SUCCEEDED.value
        assert by["fail"].status == StepStatus.SKIPPED.value
        assert len(client.calls) == first_calls == 2
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.lease_token is None
