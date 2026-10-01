"""Unit tests for flat FOR_EACH LOOP runtime (PR #50)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from app.agent.complex_plan_validator import StaticComplexPlanValidator
from app.core.errors import AppError
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    JoinPolicy,
    LoopMode,
    StepStatus,
)
from app.execution.binding_resolver import RuntimeBindingResolver
from app.execution.claim import ExecutionClaimService
from app.execution.lineage import assert_tool_step_lineage
from app.execution.loop_runtime import (
    LOOP_COLLECTION_TYPE_MISMATCH,
    LOOP_MAX_ITERATIONS_EXCEEDED,
    LOOP_RUNTIME_UNSUPPORTED,
    LOOP_TIMEOUT,
    PLAN_LIMIT_EXCEEDED,
    assert_flat_foreach_runtime_compatible,
    build_loop_body_ownership,
    hash_collection,
    iteration_step_key,
    top_level_plan_steps,
)
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
from app.schemas.plan_binding import parse_plan_binding_value
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects, _seed_ready

_AV = uuid.uuid4()
_AP = uuid.uuid4()

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {"item": {}},
    "required": ["item"],
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _OkClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, endpoint, **kwargs):
        snapshot = {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={
                    "ok": True,
                    "echo": (kwargs.get("arguments") or {}).get("item"),
                },
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _FailClient(_OkClient):
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        super().__init__()
        self._fail_on = fail_on or set()

    async def call_tool(self, endpoint, **kwargs):
        snapshot = {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()
        }
        self.calls.append({"endpoint": endpoint, **snapshot})
        n = len(self.calls)
        if n in self._fail_on:
            raise MCPClientError(
                error_layer="TRANSPORT",
                error_code="MCP_TEST_ERROR",
                message="body fail",
                retryable=False,
                outcome_unknown=False,
            )
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={
                    "ok": True,
                    "echo": (kwargs.get("arguments") or {}).get("item"),
                },
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


def _resolver_factory():
    return UnimplementedSecretResolver()


def _loop_ctx_binding(path: str = "/item") -> dict[str, Any]:
    return {"kind": BindingKind.LOOP_CONTEXT.value, "path": path}


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    bindings: dict[str, Any] | None = None,
    when: dict[str, Any] | None = None,
    on_error: str = "FAIL_EXECUTION",
    required: bool = True,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": required,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": timeout_seconds,
        "on_error": on_error,
        "config": {
            "tool_version_id": str(uuid.uuid4()),
            "bindings": bindings
            or {
                "item": _loop_ctx_binding("/item"),
            },
        },
    }


def _condition(
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


def _join(
    sid: str,
    depends_on: list[str],
    *,
    policy: str = JoinPolicy.ALL_SUCCESS.value,
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


def _loop(
    sid: str,
    *,
    body_step_ids: list[str],
    collection_path: str = "/items",
    max_iterations: int = 10,
    on_error: str = "FAIL_EXECUTION",
    timeout_seconds: int = 30,
    depends_on: list[str] | None = None,
    mode: str = LoopMode.FOR_EACH.value,
    predicate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "mode": mode,
        "max_iterations": max_iterations,
        "body_step_ids": body_step_ids,
    }
    if mode == LoopMode.FOR_EACH.value:
        cfg["collection"] = {
            "kind": BindingKind.PLAN_INPUT.value,
            "path": collection_path,
        }
    if mode == LoopMode.WHILE.value:
        cfg["predicate"] = predicate or {
            "op": "eq",
            "left": {"kind": BindingKind.LITERAL.value, "value": True},
            "right": {"kind": BindingKind.LITERAL.value, "value": True},
        }
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.LOOP.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": timeout_seconds,
        "on_error": on_error,
        "config": cfg,
    }


def _approval(sid: str, *, depends_on: list[str]) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.APPROVAL.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"approval_policy_id": str(_AP)},
    }


def _plan(
    steps: list[dict[str, Any]],
    tool_version_id: uuid.UUID,
    *,
    max_parallelism: int = 4,
    max_steps: int | None = None,
    max_loop_iterations: int | None = None,
    response_step_ids: list[str] | None = None,
) -> dict[str, Any]:
    rewritten: list[dict[str, Any]] = []
    for step in steps:
        s = dict(step)
        if s["type"] == AuthorableStepType.TOOL.value:
            cfg = dict(s.get("config") or {})
            cfg["tool_version_id"] = str(tool_version_id)
            # Body TOOL defaults to LOOP_CONTEXT; top-level TOOL uses literal.
            if s["id"] not in _body_ids_of(steps) and "item" in (
                cfg.get("bindings") or {}
            ):
                binding = (cfg.get("bindings") or {}).get("item") or {}
                if binding.get("kind") == BindingKind.LOOP_CONTEXT.value:
                    cfg["bindings"] = {
                        "item": {
                            "kind": BindingKind.LITERAL.value,
                            "value": {"id": "top"},
                        }
                    }
            s["config"] = cfg
        rewritten.append(s)
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = max_parallelism
    if max_steps is not None:
        limits["max_steps"] = max_steps
    if max_loop_iterations is not None:
        limits["max_loop_iterations"] = max_loop_iterations
    body_ids = _body_ids_of(rewritten)
    if response_step_ids is None:
        response_step_ids = [
            s["id"]
            for s in rewritten
            if s["id"] not in body_ids
            and s["type"]
            in {
                AuthorableStepType.TOOL.value,
                AuthorableStepType.LOOP.value,
                AuthorableStepType.JOIN.value,
            }
        ] or [s["id"] for s in rewritten if s["id"] not in body_ids][-1:]
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "foreach loop fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": limits,
        "steps": rewritten,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_step_ids,
        },
    }


def _body_ids_of(steps: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for s in steps:
        if s.get("type") == AuthorableStepType.LOOP.value:
            out.update((s.get("config") or {}).get("body_step_ids") or [])
    return out


def _simple_foreach_plan(
    tool_version_id: uuid.UUID,
    *,
    max_iterations: int = 10,
    on_error_body: str = "FAIL_EXECUTION",
    on_error_loop: str = "FAIL_EXECUTION",
    timeout_seconds: int = 30,
    max_steps: int | None = None,
) -> dict[str, Any]:
    return _plan(
        [
            _loop(
                "loop1",
                body_step_ids=["body"],
                max_iterations=max_iterations,
                on_error=on_error_loop,
                timeout_seconds=timeout_seconds,
            ),
            _tool("body", depends_on=["loop1"], on_error=on_error_body),
        ],
        tool_version_id,
        max_steps=max_steps,
    )


async def _seed_executable(
    session: AsyncSession,
    *,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Seed with default location schema so AgentRequest reaches READY, then
    # retarget ToolVersion.input_schema for LOOP_CONTEXT /item body bindings.
    seeded = await _seed_ready(session)
    schema = input_schema or _ITEM_SCHEMA
    await session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(input_schema=schema)
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
    input_snapshot: dict[str, Any] | None = None,
) -> uuid.UUID:
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
            trigger_type="TEST",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan_snapshot,
            plan_hash=compute_plan_hash(plan_snapshot),
            input_snapshot=input_snapshot if input_snapshot is not None else {},
            policy_snapshot=dict(seeded["policy_snapshot"]),
            requested_at=datetime.now(UTC),
            trace_id="trace-loop",
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
    input_snapshot: dict[str, Any] | None = None,
    worker_id: str = "worker-loop",
) -> tuple[uuid.UUID, Any]:
    async with db_session_factory() as session:
        execution_id = await _materialize_queued(
            session,
            plan_snapshot=plan,
            seeded=seeded,
            input_snapshot=input_snapshot,
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


def _child_by_template(
    steps: list[Any], *, template_id: str, iteration_no: int
) -> Any:
    matches = []
    for s in steps:
        if s.parent_step_id is None or s.iteration_no != iteration_no:
            continue
        snap = s.step_snapshot or {}
        if snap.get("id") == template_id:
            matches.append(s)
    assert len(matches) == 1, (template_id, iteration_no, matches)
    return matches[0]


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_excludes_body_templates(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _simple_foreach_plan(seeded["tool_version_id"])
        outcome = await ExecutionPlanMaterializer(session).materialize(
            ExecutionMaterializeParams(
                source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
                trigger_type="TEST",
                requester_id=seeded["requester_id"],
                agent_version_id=seeded["agent_version_id"],
                plan_snapshot=plan,
                plan_hash=compute_plan_hash(plan),
                input_snapshot={"items": [{"id": 1}]},
                policy_snapshot=dict(seeded["policy_snapshot"]),
                requested_at=datetime.now(UTC),
            )
        )
        await session.commit()
        by = {s.step_key: s for s in outcome.steps}
        assert set(by) == {"loop1"}
        assert "body" not in by
        assert by["loop1"].parent_step_id is None
        assert by["loop1"].iteration_no is None
        assert by["loop1"].mcp_tool_version_id is None
        assert by["loop1"].status == StepStatus.PENDING.value


@pytest.mark.asyncio
async def test_materialize_non_loop_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        plan = _plan(
            [
                _tool(
                    "a",
                    bindings={
                        "item": {
                            "kind": BindingKind.LITERAL.value,
                            "value": {"id": 1},
                        }
                    },
                ),
                _tool(
                    "b",
                    depends_on=["a"],
                    bindings={
                        "item": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "a",
                            "path": "/structured_content/echo",
                        }
                    },
                ),
            ],
            seeded["tool_version_id"],
        )
        outcome = await ExecutionPlanMaterializer(session).materialize(
            ExecutionMaterializeParams(
                source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
                trigger_type="TEST",
                requester_id=seeded["requester_id"],
                agent_version_id=seeded["agent_version_id"],
                plan_snapshot=plan,
                plan_hash=compute_plan_hash(plan),
                input_snapshot={},
                policy_snapshot=dict(seeded["policy_snapshot"]),
                requested_at=datetime.now(UTC),
            )
        )
        await session.commit()
        assert [s.step_key for s in outcome.steps] == ["a", "b"]
        assert all(s.parent_step_id is None for s in outcome.steps)
        assert all(s.iteration_no is None for s in outcome.steps)


# ---------------------------------------------------------------------------
# Deterministic iteration keys
# ---------------------------------------------------------------------------


def test_iteration_step_key_deterministic() -> None:
    parent = uuid.uuid4()
    k1 = iteration_step_key(
        parent_step_id=parent, iteration_no=1, template_step_id="body"
    )
    k2 = iteration_step_key(
        parent_step_id=parent, iteration_no=1, template_step_id="body"
    )
    k3 = iteration_step_key(
        parent_step_id=parent, iteration_no=2, template_step_id="body"
    )
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith(f"loop:{parent.hex}:000001:")
    with pytest.raises(AppError):
        iteration_step_key(
            parent_step_id=parent, iteration_no=0, template_step_id="body"
        )


@pytest.mark.asyncio
async def test_iteration_rows_parent_and_iteration_no(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    items = [{"id": 1}, {"id": 2}]
    plan = _simple_foreach_plan(seeded["tool_version_id"])
    client = _OkClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan,
        seeded=seeded,
        client=client,
        input_snapshot={"items": items},
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        children = [s for s in steps if s.parent_step_id == loop.id]
        assert len(children) == 2
        for child in children:
            assert child.iteration_no in {1, 2}
            assert child.parent_step_id == loop.id
            assert child.step_key == iteration_step_key(
                parent_step_id=loop.id,
                iteration_no=child.iteration_no,
                template_step_id="body",
            )
            assert child.step_snapshot["id"] == "body"


# ---------------------------------------------------------------------------
# Static ComplexPlanValidator / ownership rules
# ---------------------------------------------------------------------------


def test_static_top_level_depends_on_body_rejected() -> None:
    plan = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "static",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": [
            _loop("loop1", body_step_ids=["body"]),
            _tool("body", depends_on=["loop1"]),
            _tool(
                "after",
                depends_on=["body"],
                bindings={
                    "item": {"kind": BindingKind.LITERAL.value, "value": {"id": 1}}
                },
            ),
        ],
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": ["loop1"],
        },
    }
    # Rewrite tool_version_id consistently
    tv = uuid.uuid4()
    for s in plan["steps"]:
        if s["type"] == "TOOL":
            s["config"]["tool_version_id"] = str(tv)
    result = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(plan)
    )
    assert not result.ok
    assert any("body template" in e.message for e in result.errors)


def test_static_top_level_step_output_body_rejected() -> None:
    tv = uuid.uuid4()
    plan = _plan(
        [
            _loop("loop1", body_step_ids=["body"]),
            _tool("body", depends_on=["loop1"]),
            _tool(
                "after",
                depends_on=["loop1"],
                bindings={
                    "item": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "body",
                        "path": "/structured_content/echo",
                    }
                },
            ),
        ],
        tv,
        response_step_ids=["loop1"],
    )
    result = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(plan)
    )
    assert not result.ok
    assert "PLAN_BINDING_INVALID" in result.error_codes


def test_static_response_step_ids_body_rejected() -> None:
    tv = uuid.uuid4()
    plan = _plan(
        [
            _loop("loop1", body_step_ids=["body"]),
            _tool("body", depends_on=["loop1"]),
        ],
        tv,
        response_step_ids=["body"],
    )
    result = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(plan)
    )
    assert not result.ok
    assert any("body template" in e.message for e in result.errors)


def test_static_body_same_loop_ancestor_step_output_ok() -> None:
    tv = uuid.uuid4()
    plan = _plan(
        [
            _loop("loop1", body_step_ids=["a", "b"]),
            _tool("a", depends_on=["loop1"]),
            _tool(
                "b",
                depends_on=["a"],
                bindings={
                    "item": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/structured_content/echo",
                    }
                },
            ),
        ],
        tv,
        response_step_ids=["loop1"],
    )
    result = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(plan)
    )
    assert result.ok, result.errors


def test_static_body_forward_and_sibling_step_output_rejected() -> None:
    tv = uuid.uuid4()
    # Forward: a depends_on loop1 but STEP_OUTPUT from b (not an ancestor).
    plan_fwd = _plan(
        [
            _loop("loop1", body_step_ids=["a", "b"]),
            _tool(
                "a",
                depends_on=["loop1"],
                bindings={
                    "item": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "b",
                        "path": "/structured_content/echo",
                    }
                },
            ),
            _tool("b", depends_on=["a"]),
        ],
        tv,
        response_step_ids=["loop1"],
    )
    r1 = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(plan_fwd)
    )
    assert not r1.ok
    assert "PLAN_BINDING_INVALID" in r1.error_codes

    # Sibling: both depend only on loop; neither is ancestor of the other.
    plan_sib = _plan(
        [
            _loop("loop1", body_step_ids=["a", "b"]),
            _tool("a", depends_on=["loop1"]),
            _tool(
                "b",
                depends_on=["loop1"],
                bindings={
                    "item": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/structured_content/echo",
                    }
                },
            ),
        ],
        tv,
        response_step_ids=["loop1"],
    )
    r2 = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(plan_sib)
    )
    assert not r2.ok
    assert "PLAN_BINDING_INVALID" in r2.error_codes


def test_static_other_loop_step_output_rejected() -> None:
    tv = uuid.uuid4()
    plan = _plan(
        [
            _loop("loop1", body_step_ids=["a1"]),
            _tool("a1", depends_on=["loop1"]),
            _loop("loop2", body_step_ids=["a2"], depends_on=["loop1"]),
            _tool(
                "a2",
                depends_on=["loop2"],
                bindings={
                    "item": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a1",
                        "path": "/structured_content/echo",
                    }
                },
            ),
        ],
        tv,
        response_step_ids=["loop2"],
    )
    result = StaticComplexPlanValidator().validate(
        ExecutionPlanV1.model_validate(plan)
    )
    assert not result.ok
    assert "PLAN_BINDING_INVALID" in result.error_codes


# ---------------------------------------------------------------------------
# LOOP_CONTEXT resolution
# ---------------------------------------------------------------------------


def _foreach_binding_plan(tv: uuid.UUID) -> ExecutionPlanV1:
    return ExecutionPlanV1.model_validate(
        _plan(
            [
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
            ],
            tv,
            response_step_ids=["loop1"],
        )
    )


def _ns_exec(**kwargs: Any) -> Any:
    base = {
        "id": uuid.uuid4(),
        "source_type": ExecutionSourceType.MANUAL_TOOL_TEST.value,
        "trigger_type": "TEST",
        "trace_id": "t",
        "input_snapshot": {"items": [{"id": 1}, {"id": 2}]},
        "status": ExecutionStatus.RUNNING.value,
        "plan_snapshot": None,
        "plan_hash": None,
        "plan_schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _ns_step(**kwargs: Any) -> Any:
    return SimpleNamespace(**kwargs)


def test_loop_context_item_index_iteration_nested_pointer() -> None:
    tv = uuid.uuid4()
    plan = _foreach_binding_plan(tv)
    items = [{"id": 1, "nested": {"x": 9}}, {"id": 2}]
    execution = _ns_exec(input_snapshot={"items": items})
    loop_id = uuid.uuid4()
    loop_snap = next(s for s in plan.steps if s.id == "loop1").model_dump(mode="json")
    body_snap = next(s for s in plan.steps if s.id == "body").model_dump(mode="json")
    loop = _ns_step(
        id=loop_id,
        execution_id=execution.id,
        step_key="loop1",
        step_type=AuthorableStepType.LOOP.value,
        parent_step_id=None,
        iteration_no=None,
        status=StepStatus.RUNNING.value,
        step_snapshot=loop_snap,
        resolved_input={
            "mode": LoopMode.FOR_EACH.value,
            "collection_hash": hash_collection(items),
            "collection_size": len(items),
        },
    )
    child = _ns_step(
        id=uuid.uuid4(),
        execution_id=execution.id,
        step_key=iteration_step_key(
            parent_step_id=loop_id, iteration_no=1, template_step_id="body"
        ),
        step_type=AuthorableStepType.TOOL.value,
        parent_step_id=loop_id,
        iteration_no=1,
        status=StepStatus.PENDING.value,
        step_snapshot=body_snap,
        resolved_input=None,
        result_inline=None,
    )
    by_key = {loop.step_key: loop, child.step_key: child}
    resolver = RuntimeBindingResolver()
    ancestors = resolver.transitive_ancestors(plan)
    for path, expected in (
        ("/item", items[0]),
        ("/index", 0),
        ("/iteration_no", 1),
        ("/collection_size", 2),
        ("/item/nested/x", 9),
    ):
        value = resolver.resolve_binding(
            binding=parse_plan_binding_value(
                {"kind": BindingKind.LOOP_CONTEXT.value, "path": path}
            ),
            execution=execution,
            owning_step=child,
            by_key=by_key,
            plan=plan,
            ancestors=ancestors,
            missing_ok=False,
        )
        assert value == expected


def test_loop_context_outside_body_and_malformed_parent_fail() -> None:
    tv = uuid.uuid4()
    plan = _foreach_binding_plan(tv)
    execution = _ns_exec()
    top = _ns_step(
        id=uuid.uuid4(),
        execution_id=execution.id,
        step_key="loop1",
        step_type=AuthorableStepType.LOOP.value,
        parent_step_id=None,
        iteration_no=None,
        status=StepStatus.RUNNING.value,
        step_snapshot=next(s for s in plan.steps if s.id == "loop1").model_dump(
            mode="json"
        ),
        resolved_input={
            "mode": LoopMode.FOR_EACH.value,
            "collection_hash": hash_collection(execution.input_snapshot["items"]),
            "collection_size": 2,
        },
    )
    resolver = RuntimeBindingResolver()
    ancestors = resolver.transitive_ancestors(plan)
    with pytest.raises(AppError) as outside:
        resolver.resolve_binding(
            binding=parse_plan_binding_value(_loop_ctx_binding()),
            execution=execution,
            owning_step=top,
            by_key={top.step_key: top},
            plan=plan,
            ancestors=ancestors,
        )
    assert "LOOP_CONTEXT" in outside.value.message

    body_snap = next(s for s in plan.steps if s.id == "body").model_dump(mode="json")
    orphan = _ns_step(
        id=uuid.uuid4(),
        execution_id=execution.id,
        step_key="orphan",
        step_type=AuthorableStepType.TOOL.value,
        parent_step_id=uuid.uuid4(),  # missing parent
        iteration_no=1,
        status=StepStatus.PENDING.value,
        step_snapshot=body_snap,
    )
    with pytest.raises(AppError) as missing_parent:
        resolver.resolve_binding(
            binding=parse_plan_binding_value(_loop_ctx_binding()),
            execution=execution,
            owning_step=orphan,
            by_key={orphan.step_key: orphan},
            plan=plan,
            ancestors=ancestors,
        )
    assert "parent" in missing_parent.value.message.lower()


def test_loop_context_collection_hash_drift_fail() -> None:
    tv = uuid.uuid4()
    plan = _foreach_binding_plan(tv)
    items = [{"id": 1}, {"id": 2}]
    execution = _ns_exec(input_snapshot={"items": items})
    loop_id = uuid.uuid4()
    loop = _ns_step(
        id=loop_id,
        execution_id=execution.id,
        step_key="loop1",
        step_type=AuthorableStepType.LOOP.value,
        parent_step_id=None,
        iteration_no=None,
        status=StepStatus.RUNNING.value,
        step_snapshot=next(s for s in plan.steps if s.id == "loop1").model_dump(
            mode="json"
        ),
        resolved_input={
            "mode": LoopMode.FOR_EACH.value,
            "collection_hash": "drifted-hash",
            "collection_size": 2,
        },
    )
    child = _ns_step(
        id=uuid.uuid4(),
        execution_id=execution.id,
        step_key=iteration_step_key(
            parent_step_id=loop_id, iteration_no=1, template_step_id="body"
        ),
        step_type=AuthorableStepType.TOOL.value,
        parent_step_id=loop_id,
        iteration_no=1,
        status=StepStatus.PENDING.value,
        step_snapshot=next(s for s in plan.steps if s.id == "body").model_dump(
            mode="json"
        ),
    )
    resolver = RuntimeBindingResolver()
    with pytest.raises(AppError) as exc:
        resolver.resolve_binding(
            binding=parse_plan_binding_value(_loop_ctx_binding()),
            execution=execution,
            owning_step=child,
            by_key={loop.step_key: loop, child.step_key: child},
            plan=plan,
            ancestors=resolver.transitive_ancestors(plan),
        )
    assert "drift" in exc.value.message.lower()


# ---------------------------------------------------------------------------
# Runtime via orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_collection_loop_success_zero_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _simple_foreach_plan(seeded["tool_version_id"])
    client = _OkClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan,
        seeded=seeded,
        client=client,
        input_snapshot={"items": []},
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 0
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        loop = steps[0]
        assert loop.status == StepStatus.SUCCEEDED.value
        assert loop.result_inline == {
            "mode": LoopMode.FOR_EACH.value,
            "iterations_completed": 0,
            "collection_size": 0,
        }
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_one_and_three_items_sequential_body_item(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    for items in ([{"id": 1}], [{"id": 1}, {"id": 2}, {"id": 3}]):
        client = _OkClient()
        plan = _simple_foreach_plan(seeded["tool_version_id"])
        execution_id, outcome = await _claim_and_run(
            db_session_factory,
            plan=plan,
            seeded=seeded,
            client=client,
            input_snapshot={"items": items},
            worker_id=f"w-{len(items)}",
        )
        assert outcome.reason == "EXECUTION_SUCCEEDED"
        assert len(client.calls) == len(items)
        echoed = [c.get("arguments", {}).get("item") for c in client.calls]
        assert echoed == items
        async with db_session_factory() as session:
            steps = await ExecutionRepository(session).list_steps(execution_id)
            loop = next(s for s in steps if s.step_key == "loop1")
            assert loop.status == StepStatus.SUCCEEDED.value
            children = [s for s in steps if s.parent_step_id == loop.id]
            assert len(children) == len(items)
            assert {c.iteration_no for c in children} == set(range(1, len(items) + 1))
            # Sequential: only one child RUNNING/READY at a time historically —
            # all terminal SUCCEEDED now; MCP call order matches iteration order.
            assert [c.iteration_no for c in sorted(children, key=lambda s: s.iteration_no)] == list(
                range(1, len(items) + 1)
            )


@pytest.mark.asyncio
async def test_body_condition_when_true_false_and_join(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # when=true branch runs TOOL; when=false skips TOOL.
    plan_true = _plan(
        [
            _loop("loop1", body_step_ids=["c", "t", "j"]),
            _condition(
                "c",
                {
                    "op": "eq",
                    "left": {"kind": BindingKind.LITERAL.value, "value": True},
                    "right": {"kind": BindingKind.LITERAL.value, "value": True},
                },
                depends_on=["loop1"],
            ),
            _tool(
                "t",
                depends_on=["c"],
                when={
                    "op": "eq",
                    "left": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "c",
                        "path": "/condition_result",
                    },
                    "right": {"kind": BindingKind.LITERAL.value, "value": True},
                },
            ),
            _join("j", ["t"], policy=JoinPolicy.ALL_COMPLETE.value),
        ],
        seeded["tool_version_id"],
        response_step_ids=["loop1"],
    )
    client = _OkClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan_true,
        seeded=seeded,
        client=client,
        input_snapshot={"items": [{"id": 1}]},
        worker_id="w-cond-true",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    assert len(client.calls) == 1
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        c = _child_by_template(steps, template_id="c", iteration_no=1)
        t = _child_by_template(steps, template_id="t", iteration_no=1)
        j = _child_by_template(steps, template_id="j", iteration_no=1)
        assert c.status == StepStatus.SUCCEEDED.value
        assert c.condition_result is True
        assert t.status == StepStatus.SUCCEEDED.value
        assert j.status == StepStatus.SUCCEEDED.value

    plan_false = _plan(
        [
            _loop("loop1", body_step_ids=["c", "t"]),
            _condition(
                "c",
                {
                    "op": "eq",
                    "left": {"kind": BindingKind.LITERAL.value, "value": False},
                    "right": {"kind": BindingKind.LITERAL.value, "value": True},
                },
                depends_on=["loop1"],
            ),
            _tool(
                "t",
                depends_on=["c"],
                when={
                    "op": "eq",
                    "left": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "c",
                        "path": "/condition_result",
                    },
                    "right": {"kind": BindingKind.LITERAL.value, "value": True},
                },
            ),
        ],
        seeded["tool_version_id"],
        response_step_ids=["loop1"],
    )
    client2 = _OkClient()
    execution_id2, outcome2 = await _claim_and_run(
        db_session_factory,
        plan=plan_false,
        seeded=seeded,
        client=client2,
        input_snapshot={"items": [{"id": 1}]},
        worker_id="w-cond-false",
    )
    assert outcome2.reason == "EXECUTION_SUCCEEDED"
    assert len(client2.calls) == 0
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id2)
        c = _child_by_template(steps, template_id="c", iteration_no=1)
        t = _child_by_template(steps, template_id="t", iteration_no=1)
        assert c.condition_result is False
        assert t.status == StepStatus.SKIPPED.value
        assert t.error_code == "STEP_WHEN_FALSE"


@pytest.mark.asyncio
async def test_continue_mark_partial_fail_execution_body_errors(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    items = [{"id": 1}, {"id": 2}, {"id": 3}]

    # CONTINUE: fail first MCP; still advances remaining iterations.
    client_c = _FailClient(fail_on={1})
    plan_c = _simple_foreach_plan(
        seeded["tool_version_id"], on_error_body="CONTINUE"
    )
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan_c,
        seeded=seeded,
        client=client_c,
        input_snapshot={"items": items},
        worker_id="w-cont",
    )
    assert len(client_c.calls) == 3
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        assert loop.status == StepStatus.SUCCEEDED.value
        children = sorted(
            [s for s in steps if s.parent_step_id == loop.id],
            key=lambda s: s.iteration_no or 0,
        )
        assert children[0].status == StepStatus.FAILED.value
        assert children[1].status == StepStatus.SUCCEEDED.value
        assert children[2].status == StepStatus.SUCCEEDED.value

    # MARK_PARTIAL retained through completion.
    client_m = _FailClient(fail_on={1})
    plan_m = _simple_foreach_plan(
        seeded["tool_version_id"], on_error_body="MARK_PARTIAL"
    )
    execution_id_m, _ = await _claim_and_run(
        db_session_factory,
        plan=plan_m,
        seeded=seeded,
        client=client_m,
        input_snapshot={"items": items},
        worker_id="w-mark",
    )
    assert len(client_m.calls) == 3
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id_m)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value

    # FAIL_EXECUTION stops before further iterations.
    client_f = _FailClient(fail_on={1})
    plan_f = _simple_foreach_plan(
        seeded["tool_version_id"], on_error_body="FAIL_EXECUTION"
    )
    execution_id_f, _ = await _claim_and_run(
        db_session_factory,
        plan=plan_f,
        seeded=seeded,
        client=client_f,
        input_snapshot={"items": items},
        worker_id="w-fail",
    )
    assert len(client_f.calls) == 1
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id_f)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = await ExecutionRepository(session).list_steps(execution_id_f)
        children = [s for s in steps if s.parent_step_id is not None]
        assert len(children) == 1
        assert children[0].status == StepStatus.FAILED.value


@pytest.mark.asyncio
async def test_collection_wrong_type_max_iterations_expanded_steps_timeout(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # Wrong collection type.
    client = _OkClient()
    plan = _simple_foreach_plan(seeded["tool_version_id"])
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan,
        seeded=seeded,
        client=client,
        input_snapshot={"items": {"not": "a-list"}},
        worker_id="w-type",
    )
    assert len(client.calls) == 0
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == LOOP_COLLECTION_TYPE_MISMATCH
        loop = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert loop.error_code == LOOP_COLLECTION_TYPE_MISMATCH

    # max_iterations exceeded — no body MCP.
    client2 = _OkClient()
    plan2 = _simple_foreach_plan(seeded["tool_version_id"], max_iterations=2)
    execution_id2, _ = await _claim_and_run(
        db_session_factory,
        plan=plan2,
        seeded=seeded,
        client=client2,
        input_snapshot={"items": [{"id": 1}, {"id": 2}, {"id": 3}]},
        worker_id="w-maxiter",
    )
    assert len(client2.calls) == 0
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id2)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        loop = next(
            s
            for s in await ExecutionRepository(session).list_steps(execution_id2)
            if s.step_key == "loop1"
        )
        assert loop.error_code == LOOP_MAX_ITERATIONS_EXCEEDED
        assert all(s.parent_step_id is None for s in await ExecutionRepository(session).list_steps(execution_id2))

    # Expanded projected steps exceed limits.max_steps.
    client3 = _OkClient()
    # top_level=1 (loop) + 3*1 body = 4 > max_steps=3
    plan3 = _simple_foreach_plan(seeded["tool_version_id"], max_steps=3)
    execution_id3, _ = await _claim_and_run(
        db_session_factory,
        plan=plan3,
        seeded=seeded,
        client=client3,
        input_snapshot={"items": [{"id": 1}, {"id": 2}, {"id": 3}]},
        worker_id="w-maxsteps",
    )
    assert len(client3.calls) == 0
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id3)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == PLAN_LIMIT_EXCEEDED

    # LOOP timeout after start.
    client4 = _OkClient()
    plan4 = _simple_foreach_plan(
        seeded["tool_version_id"], timeout_seconds=1
    )
    async with db_session_factory() as session:
        execution_id4 = await _materialize_queued(
            session,
            plan_snapshot=plan4,
            seeded=seeded,
            input_snapshot={"items": [{"id": 1}, {"id": 2}]},
        )
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id4, worker_id="w-timeout"
        )
        await session.commit()
        assert claim.lease_token is not None
        lease = claim.lease_token

    from app.execution.orchestrator import ExecutionOrchestrator

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=client4,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    orch = ExecutionOrchestrator(
        session_factory=db_session_factory, tool_runner=runner
    )
    # First prepare: start LOOP + materialize iter 1.
    wave1 = await orch._prepare_wave(
        execution_id=execution_id4,
        worker_id="w-timeout",
        lease_token=lease,
    )
    assert wave1.reason in {"WAVE_READY", "NO_READY", "LOOP_CHANGED"}
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id4)
        loop = next(s for s in steps if s.step_key == "loop1")
        assert loop.status == StepStatus.RUNNING.value
        loop.started_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

    wave2 = await orch._prepare_wave(
        execution_id=execution_id4,
        worker_id="w-timeout",
        lease_token=lease,
    )
    assert wave2.execution_complete or wave2.reason in {
        "EXECUTION_FAILED",
        "EXECUTION_TIMED_OUT",
        StepStatus.TIMED_OUT.value,
        StepStatus.FAILED.value,
    }
    async with db_session_factory() as session:
        loop = next(
            s
            for s in await ExecutionRepository(session).list_steps(execution_id4)
            if s.step_key == "loop1"
        )
        assert loop.status == StepStatus.TIMED_OUT.value
        assert loop.error_code == LOOP_TIMEOUT
        execution = await ExecutionRepository(session).get(execution_id4)
        assert execution is not None
        assert execution.status == ExecutionStatus.TIMED_OUT.value
    assert len(client4.calls) == 0


@pytest.mark.asyncio
async def test_while_nested_loop_body_approval_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    # WHILE — assert_flat_foreach_runtime_compatible / DAG fail-closed.
    while_plan = _plan(
        [
            _loop(
                "loop1",
                body_step_ids=["body"],
                mode=LoopMode.WHILE.value,
                predicate={
                    "op": "eq",
                    "left": {"kind": BindingKind.LITERAL.value, "value": True},
                    "right": {"kind": BindingKind.LITERAL.value, "value": True},
                },
            ),
            _tool(
                "body",
                depends_on=["loop1"],
                bindings={
                    "item": {"kind": BindingKind.LITERAL.value, "value": {"id": 1}}
                },
            ),
        ],
        seeded["tool_version_id"],
        response_step_ids=["loop1"],
    )
    parsed = ExecutionPlanV1.model_validate(while_plan)
    with pytest.raises(AppError) as while_exc:
        assert_flat_foreach_runtime_compatible(parsed)
    assert while_exc.value.code == LOOP_RUNTIME_UNSUPPORTED

    # Claim fail-closes before MCP (DAG validates assert_flat_foreach).
    client = _OkClient()
    async with db_session_factory() as session:
        execution_id = await _materialize_queued(
            session,
            plan_snapshot=while_plan,
            seeded=seeded,
            input_snapshot={},
        )
        with pytest.raises(AppError) as claim_exc:
            await ExecutionClaimService(session, lease_seconds=120).claim(
                execution_id=execution_id, worker_id="w-while"
            )
        assert claim_exc.value.code == LOOP_RUNTIME_UNSUPPORTED
        await session.rollback()
    assert len(client.calls) == 0

    # Nested LOOP body template (inner LOOP owned by outer).
    nested = _plan(
        [
            _loop("outer", body_step_ids=["inner"]),
            _loop("inner", body_step_ids=["ibody"], depends_on=["outer"]),
            _tool(
                "ibody",
                depends_on=["inner"],
                bindings={
                    "item": {"kind": BindingKind.LITERAL.value, "value": {"id": 1}}
                },
            ),
        ],
        seeded["tool_version_id"],
        response_step_ids=["outer"],
    )
    with pytest.raises(AppError) as nested_exc:
        assert_flat_foreach_runtime_compatible(ExecutionPlanV1.model_validate(nested))
    assert nested_exc.value.code == LOOP_RUNTIME_UNSUPPORTED

    # Body APPROVAL.
    appr = _plan(
        [
            _loop("loop1", body_step_ids=["apr"]),
            _approval("apr", depends_on=["loop1"]),
        ],
        seeded["tool_version_id"],
        response_step_ids=["loop1"],
    )
    with pytest.raises(AppError) as appr_exc:
        assert_flat_foreach_runtime_compatible(ExecutionPlanV1.model_validate(appr))
    assert appr_exc.value.code == LOOP_RUNTIME_UNSUPPORTED
    assert "APPROVAL" in appr_exc.value.message


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lineage_child_projection_and_tamper(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()
    plan = _simple_foreach_plan(seeded["tool_version_id"])
    client = _OkClient()
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan,
        seeded=seeded,
        client=client,
        input_snapshot={"items": [{"id": 1}]},
        worker_id="w-lin",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        child = _child_by_template(steps, template_id="body", iteration_no=1)
        # Parent must be RUNNING for lineage; temporarily set for assertion after done.
        # After success parent is SUCCEEDED — lineage for RUNNING parent required.
        # Validate projection equality vs Plan template.
        plan_obj = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        expected = next(s for s in plan_obj.steps if s.id == "body")
        assert child.step_snapshot == expected.model_dump(mode="json")

        # Tamper step_key
        child.step_key = "tampered-key"
        loop.status = StepStatus.RUNNING.value
        with pytest.raises(AppError) as key_exc:
            assert_tool_step_lineage(execution, child, steps=steps)
        assert key_exc.value.code == "RESOURCE_CONFLICT"
        child.step_key = iteration_step_key(
            parent_step_id=loop.id, iteration_no=1, template_step_id="body"
        )

        # Tamper iteration_no
        child.iteration_no = 9
        with pytest.raises(AppError):
            assert_tool_step_lineage(execution, child, steps=steps)
        child.iteration_no = 1

        # Tamper parent_step_id
        child.parent_step_id = uuid.uuid4()
        with pytest.raises(AppError):
            assert_tool_step_lineage(execution, child, steps=steps)


@pytest.mark.asyncio
async def test_same_and_cross_iteration_step_output(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        seeded = await _seed_executable(session)
        await session.commit()

    plan = _plan(
        [
            _loop("loop1", body_step_ids=["a", "b"]),
            _tool("a", depends_on=["loop1"]),
            _tool(
                "b",
                depends_on=["a"],
                bindings={
                    "item": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/structured_content/echo",
                    }
                },
            ),
        ],
        seeded["tool_version_id"],
        response_step_ids=["loop1"],
    )
    client = _OkClient()
    items = [{"id": 1}, {"id": 2}]
    execution_id, outcome = await _claim_and_run(
        db_session_factory,
        plan=plan,
        seeded=seeded,
        client=client,
        input_snapshot={"items": items},
        worker_id="w-stepout",
    )
    assert outcome.reason == "EXECUTION_SUCCEEDED"
    # a + b per iteration → 4 MCP calls; b receives a's echo (the item).
    assert len(client.calls) == 4
    # a uses LOOP_CONTEXT /item; b uses same-iteration STEP_OUTPUT of a's echo.
    echoed = [c.get("arguments", {}).get("item") for c in client.calls]
    assert echoed.count(items[0]) >= 2
    assert echoed.count(items[1]) >= 2

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        steps = await ExecutionRepository(session).list_steps(execution_id)
        loop = next(s for s in steps if s.step_key == "loop1")
        a1 = _child_by_template(steps, template_id="a", iteration_no=1)
        a2 = _child_by_template(steps, template_id="a", iteration_no=2)
        b2 = _child_by_template(steps, template_id="b", iteration_no=2)
        plan_obj = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        resolver = RuntimeBindingResolver()
        by_key = {s.step_key: s for s in steps}
        ancestors = resolver.transitive_ancestors(plan_obj)

        # Same-iteration success: b2 resolves a2, not a1.
        value = resolver.resolve_binding(
            binding=parse_plan_binding_value(
                {
                    "kind": BindingKind.STEP_OUTPUT.value,
                    "step_id": "a",
                    "path": "/structured_content/echo",
                }
            ),
            execution=execution,
            owning_step=b2,
            by_key=by_key,
            plan=plan_obj,
            ancestors=ancestors,
        )
        assert value == items[1]
        assert a2.result_inline["structured_content"]["echo"] == items[1]
        assert a1.result_inline["structured_content"]["echo"] == items[0]

        # Cross-iteration reject: remove a2 so only a1 remains for template a.
        del by_key[a2.step_key]
        with pytest.raises(AppError) as cross:
            resolver.resolve_binding(
                binding=parse_plan_binding_value(
                    {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/structured_content/echo",
                    }
                ),
                execution=execution,
                owning_step=b2,
                by_key=by_key,
                plan=plan_obj,
                ancestors=ancestors,
            )
        assert "missing" in cross.value.message.lower() or "iteration" in cross.value.message.lower()


def test_top_level_plan_steps_and_ownership_helpers() -> None:
    tv = uuid.uuid4()
    plan = ExecutionPlanV1.model_validate(
        _plan(
            [
                _loop("loop1", body_step_ids=["body"]),
                _tool("body", depends_on=["loop1"]),
                _tool(
                    "after",
                    depends_on=["loop1"],
                    bindings={
                        "item": {
                            "kind": BindingKind.LITERAL.value,
                            "value": {"id": 0},
                        }
                    },
                ),
            ],
            tv,
            response_step_ids=["after"],
        )
    )
    ownership = build_loop_body_ownership(plan)
    assert ownership.body_to_loop["body"] == "loop1"
    assert ownership.loop_to_body["loop1"] == ("body",)
    tops = [s.id for s in top_level_plan_steps(plan)]
    assert tops == ["loop1", "after"]
    # Empty body set DAG path still works for non-loop plans.
    plain = ExecutionPlanV1.model_validate(
        _plan(
            [
                _tool(
                    "only",
                    bindings={
                        "item": {
                            "kind": BindingKind.LITERAL.value,
                            "value": {"id": 1},
                        }
                    },
                )
            ],
            tv,
        )
    )
    assert [s.id for s in top_level_plan_steps(plain)] == ["only"]
