"""Unit tests for sequential ErrorPolicy + ALL_REQUIRED completion (PR #46)."""

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
    RiskClass,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.completion import aggregate_all_required
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.mcp.errors import MCPClientError
from app.models.mcp import MCPToolVersion
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


class _SequencedClient:
    def __init__(
        self,
        *,
        fail_on: set[int] | None = None,
        unknown_on: set[int] | None = None,
        timeout_on: set[int] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._fail_on = fail_on or set()
        self._unknown_on = unknown_on or set()
        self._timeout_on = timeout_on or set()

    async def call_tool(self, endpoint, **kwargs):
        snapshot = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        n = len(self.calls)
        if n in self._unknown_on:
            raise MCPClientError(
                error_layer="TIMEOUT",
                error_code="MCP_UNKNOWN",
                message="ambiguous",
                retryable=False,
                outcome_unknown=True,
            )
        if n in self._timeout_on:
            raise MCPClientError(
                error_layer="TIMEOUT",
                error_code="MCP_TIMEOUT",
                message="timed out",
                retryable=False,
                outcome_unknown=False,
            )
        if n in self._fail_on:
            raise MCPClientError(
                error_layer="TRANSPORT",
                error_code="MCP_TEST_ERROR",
                message="known failure",
                retryable=False,
                outcome_unknown=False,
            )
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True, "customer_id": "C-1"},
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    required: bool = True,
    on_error: str = "FAIL_EXECUTION",
    bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": required,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": on_error,
        "config": {
            "tool_version_id": str(uuid.uuid4()),
            "bindings": bindings
            or {
                "location": {
                    "kind": BindingKind.LITERAL.value,
                    "value": "Seoul",
                }
            },
        },
    }


def _plan(steps: list[dict[str, Any]], tool_version_id: uuid.UUID) -> dict[str, Any]:
    rewritten = []
    for step in steps:
        s = dict(step)
        cfg = dict(s.get("config") or {})
        cfg["tool_version_id"] = str(tool_version_id)
        s["config"] = cfg
        rewritten.append(s)
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "error policy",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [rewritten[-1]["id"]],
        },
    }


async def _seed(session: AsyncSession) -> dict[str, Any]:
    seeded = await _seed_ready(session)
    await session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "customer_id": {"type": "string"},
                },
                "additionalProperties": False,
            }
        )
    )
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
            trace_id="trace-errpol",
        )
    )
    execution = await ExecutionRepository(session).get(outcome.execution.id)
    assert execution is not None
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    await session.flush()
    return execution.id


async def _run(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    plan_steps: list[dict[str, Any]],
    client: _SequencedClient,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[uuid.UUID, Any]:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed(session)
        plan = _plan(plan_steps, seeded["tool_version_id"])
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    return execution_id, outcome


@pytest.mark.asyncio
async def test_fail_execution_skips_downstream_and_clears_lease(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(fail_on={1})
    execution_id, outcome = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", on_error="FAIL_EXECUTION"),
            _tool("b", depends_on=["a"], on_error="FAIL_EXECUTION"),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    assert outcome.terminal_status == StepStatus.FAILED.value
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.FAILED.value
        assert steps["b"].status == StepStatus.SKIPPED.value
        assert steps["b"].error_code == "UPSTREAM_EXECUTION_STOPPED"
        assert len(await ExecutionRepository(session).list_attempts(steps["b"].id)) == 0


@pytest.mark.asyncio
async def test_fail_execution_timeout_maps_execution_timed_out(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(timeout_on={1})
    execution_id, outcome = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", on_error="FAIL_EXECUTION"),
            _tool("b", depends_on=["a"]),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    assert outcome.terminal_status == StepStatus.TIMED_OUT.value
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.TIMED_OUT.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.TIMED_OUT.value
        assert steps["b"].status == StepStatus.SKIPPED.value


@pytest.mark.asyncio
async def test_mark_partial_required_failure_then_success(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(fail_on={1})
    execution_id, _outcome = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", required=True, on_error="MARK_PARTIAL"),
            _tool("b", depends_on=["a"], required=True, on_error="FAIL_EXECUTION"),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    assert len(client.calls) == 2

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
        assert execution.lease_token is None
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.FAILED.value
        assert steps["b"].status == StepStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_mark_partial_optional_failure_still_partial(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(fail_on={1})
    execution_id, _ = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", required=False, on_error="MARK_PARTIAL"),
            _tool("b", depends_on=["a"], required=True),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value


@pytest.mark.asyncio
async def test_continue_optional_failure_final_succeeded(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(fail_on={1})
    execution_id, _ = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", required=False, on_error="CONTINUE"),
            _tool("b", depends_on=["a"], required=True),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    assert len(client.calls) == 2
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_continue_required_failure_final_partial(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(fail_on={1})
    execution_id, _ = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", required=True, on_error="CONTINUE"),
            _tool("b", depends_on=["a"], required=True),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value


@pytest.mark.asyncio
async def test_continue_all_required_failed_final_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(fail_on={1, 2})
    execution_id, _ = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", required=True, on_error="CONTINUE"),
            _tool("b", depends_on=["a"], required=True, on_error="CONTINUE"),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    assert len(client.calls) == 2
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.FAILED.value
        assert steps["b"].status == StepStatus.FAILED.value


@pytest.mark.asyncio
async def test_continue_step_output_from_failed_a_fails_b_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SequencedClient(fail_on={1})
    execution_id, outcome = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", required=True, on_error="CONTINUE"),
            _tool(
                "b",
                depends_on=["a"],
                required=True,
                bindings={
                    "customer_id": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/structured_content/customer_id",
                    }
                },
            ),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    assert len(client.calls) == 1  # B MCP 0
    assert outcome.mcp_called is False or len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.FAILED.value
        assert steps["b"].status == StepStatus.FAILED.value
        assert len(await ExecutionRepository(session).list_attempts(steps["b"].id)) == 0


@pytest.mark.asyncio
async def test_unknown_outcome_ignores_continue_and_skips_b(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNKNOWN_OUTCOME requires unsafe risk_class so classify does not soften it."""
    _install_no_side_effects(monkeypatch)
    client = _SequencedClient(unknown_on={1})
    async with db_session_factory() as session:
        seeded = await _seed(session)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(
            seeded["tool_id"]
        )
        assert policy is not None
        policy.risk_class = RiskClass.NON_IDEMPOTENT_WRITE.value
        approval = None
        if policy.approval_policy_id is not None:
            approval = await ApprovalPolicyRepository(session).get(
                policy.approval_policy_id
            )
        seeded["policy_snapshot"] = build_safe_tool_policy_snapshot(policy, approval)
        plan = _plan(
            [
                _tool("a", required=True, on_error="CONTINUE"),
                _tool("b", depends_on=["a"], required=True),
            ],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.UNKNOWN_OUTCOME.value
    assert len(client.calls) == 1

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.UNKNOWN_OUTCOME.value
        assert steps["b"].status == StepStatus.SKIPPED.value


@pytest.mark.asyncio
async def test_precondition_fatal_ignores_continue(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding precondition failure with CONTINUE must not run B."""
    client = _SequencedClient()
    # A succeeds; B has STEP_OUTPUT from missing path → fatal precondition.
    execution_id, outcome = await _run(
        db_session_factory,
        plan_steps=[
            _tool("a", required=True, on_error="CONTINUE"),
            _tool(
                "b",
                depends_on=["a"],
                required=True,
                on_error="CONTINUE",
                bindings={
                    "customer_id": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/structured_content/does_not_exist",
                    }
                },
            ),
            _tool("c", depends_on=["b"], required=True, on_error="CONTINUE"),
        ],
        client=client,
        monkeypatch=monkeypatch,
    )
    assert len(client.calls) == 1  # A only
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["a"].status == StepStatus.SUCCEEDED.value
        assert steps["b"].status == StepStatus.FAILED.value
        assert steps["c"].status == StepStatus.SKIPPED.value


@pytest.mark.asyncio
async def test_duplicate_progress_after_continuable_failure_promotes_once(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.execution.orchestrator import ExecutionOrchestrator

    _install_no_side_effects(monkeypatch)
    client = _SequencedClient(fail_on={1})
    async with db_session_factory() as session:
        seeded = await _seed(session)
        plan = _plan(
            [
                _tool("a", on_error="CONTINUE"),
                _tool("b", depends_on=["a"]),
            ],
            seeded["tool_version_id"],
        )
        execution_id = await _materialize_queued(
            session, plan_snapshot=plan, seeded=seeded
        )
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="worker-a"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    # Run A only via step runner, then progress twice.
    steps_before = None
    async with db_session_factory() as session:
        steps_before = await ExecutionRepository(session).list_steps(execution_id)
        a_id = next(s.id for s in steps_before if s.step_key == "a")

    outcome_a = await runner.run_claimed_tool_step(
        execution_id=execution_id,
        step_execution_id=a_id,
        worker_id="worker-a",
        lease_token=lease_token,
    )
    assert outcome_a.terminal_status == StepStatus.FAILED.value
    assert outcome_a.disposition == "KNOWN_STEP_FAILURE"

    orch = ExecutionOrchestrator(
        session_factory=db_session_factory, tool_runner=runner
    )
    first = await orch._progress_after_terminal_step(
        execution_id=execution_id,
        completed_step_id=a_id,
        worker_id="worker-a",
        lease_token=lease_token,
        allow_continuable_failure=True,
    )
    second = await orch._progress_after_terminal_step(
        execution_id=execution_id,
        completed_step_id=a_id,
        worker_id="worker-a",
        lease_token=lease_token,
        allow_continuable_failure=True,
    )
    assert first.promoted is True
    assert second.reason == "ALREADY_READY"
    assert second.promoted is False

    async with db_session_factory() as session:
        steps = {
            s.step_key: s
            for s in await ExecutionRepository(session).list_steps(execution_id)
        }
        assert steps["b"].status == StepStatus.READY.value
        assert len(await ExecutionRepository(session).list_attempts(steps["b"].id)) == 0


def test_aggregate_all_required_pure() -> None:
    from types import SimpleNamespace

    plan = ExecutionPlanV1.model_validate(
        _plan(
            [
                _tool("a", required=True, on_error="CONTINUE"),
                _tool("b", depends_on=["a"], required=True),
            ],
            uuid.uuid4(),
        )
    )
    steps = [
        SimpleNamespace(step_key="a", status=StepStatus.FAILED.value),
        SimpleNamespace(step_key="b", status=StepStatus.SUCCEEDED.value),
    ]
    decision = aggregate_all_required(plan=plan, steps=steps)  # type: ignore[arg-type]
    assert decision.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
