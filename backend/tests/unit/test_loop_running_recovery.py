"""Unit tests for Issue #117 expired RUNNING flat FOR_EACH / WHILE recovery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    ExecutionStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
)
from app.execution.loop_reconcile import (
    build_while_evidence_object,
    materialize_iteration,
    pin_collection_evidence,
    pin_while_control_evidence,  # used by empty-history fail-closed plant
    resolve_foreach_collection,
    succeed_loop,
    succeed_while_loop,
)
from app.execution.loop_runtime import (
    append_or_replay_while_history,
    hash_while_predicate_evidence,
    iteration_step_key,
    parse_foreach_collection_pin,
    parse_while_predicate_history,
    tool_version_for_template,
)
from app.execution.recovery import ExecutionRecoveryService, RecoveryDecision
from app.execution.workflow_dag_recovery import plan_supports_workflow_running_recovery
from app.repositories.execution import ExecutionRepository
from app.schemas.execution_plan import (
    ExecutionPlanStep,
    ExecutionPlanV1,
    LoopStepConfigV1,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_workflow_dag_recovery import (
    _expire_lease,
    _nested_loop_plan,
    _NeverClient,
    _OkClient,
    _pin_all_tool_policies,
    _pin_step_risk,
    _plant_started_toolcall,
    _runner,
    _seed_workflow_execution,
    _start_attempt_no_toolcall,
)
from tests.unit.test_workflow_registry import (
    _base_plan,
    _tool,
    _tool_plan,
)

# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------


def _foreach_plan(
    workflow_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    *,
    items_size: int = 2,
    on_error: str = "FAIL_EXECUTION",
    max_iterations: int = 10,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    del items_size  # collection comes from Execution inputs
    body = _tool(
        "body",
        tool_version_id=tool_version_id,
        depends_on=["loop1"],
        bindings={
            "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
        },
    )
    body["on_error"] = on_error
    plan = _base_plan(
        workflow_id=workflow_id,
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": timeout_seconds,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": "FOR_EACH",
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["body"],
                    "max_iterations": max_iterations,
                },
            },
            body,
        ],
        limits={"max_loop_iterations": max_iterations, "max_steps": 40},
    )
    plan["inputs"] = {
        "items": {"type": "array", "required": True, "secret": False}
    }
    plan["completion"]["response_step_ids"] = ["loop1"]
    return plan


def _while_plan(
    workflow_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    *,
    predicate: dict[str, Any] | None = None,
    on_error: str = "FAIL_EXECUTION",
    max_iterations: int = 10,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    body = _tool(
        "body",
        tool_version_id=tool_version_id,
        depends_on=["loop1"],
        bindings={
            "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
        },
    )
    body["on_error"] = on_error
    pred = predicate or {
        "op": "eq",
        "left": {"kind": BindingKind.LITERAL.value, "value": True},
        "right": {"kind": BindingKind.LITERAL.value, "value": True},
    }
    plan = _base_plan(
        workflow_id=workflow_id,
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": timeout_seconds,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": "WHILE",
                    "predicate": pred,
                    "body_step_ids": ["body"],
                    "max_iterations": max_iterations,
                },
            },
            body,
        ],
        limits={"max_loop_iterations": max_iterations, "max_steps": 40},
    )
    plan["completion"]["response_step_ids"] = ["loop1"]
    return plan


def _two_loop_plan(
    workflow_id: uuid.UUID, tool_version_id: uuid.UUID
) -> dict[str, Any]:
    def _body(sid: str, parent: str) -> dict[str, Any]:
        return _tool(
            sid,
            tool_version_id=tool_version_id,
            depends_on=[parent],
            bindings={
                "location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}
            },
        )

    plan = _base_plan(
        workflow_id=workflow_id,
        steps=[
            {
                "id": "loop_a",
                "name": "loop_a",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": "FOR_EACH",
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items_a",
                    },
                    "body_step_ids": ["body_a"],
                    "max_iterations": 10,
                },
            },
            _body("body_a", "loop_a"),
            {
                "id": "loop_b",
                "name": "loop_b",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": "FOR_EACH",
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items_b",
                    },
                    "body_step_ids": ["body_b"],
                    "max_iterations": 10,
                },
            },
            _body("body_b", "loop_b"),
        ],
        limits={"max_loop_iterations": 10, "max_steps": 40, "max_parallelism": 2},
    )
    plan["inputs"] = {
        "items_a": {"type": "array", "required": True, "secret": False},
        "items_b": {"type": "array", "required": True, "secret": False},
    }
    plan["completion"]["response_step_ids"] = ["loop_a", "loop_b"]
    return plan


def _loop_plus_top_tool_plan(
    workflow_id: uuid.UUID, tool_version_id: uuid.UUID
) -> dict[str, Any]:
    plan = _foreach_plan(workflow_id, tool_version_id, items_size=1)
    plan["steps"].append(
        _tool("top", tool_version_id=tool_version_id)
    )
    plan["limits"]["max_parallelism"] = 2
    plan["completion"]["response_step_ids"] = ["loop1", "top"]
    return plan


def _while_plus_top_tool_plan(
    workflow_id: uuid.UUID, tool_version_id: uuid.UUID
) -> dict[str, Any]:
    """WHILE (one iter then false) + independent top-level TOOL."""
    pred = {
        "op": "lt",
        "left": {
            "kind": BindingKind.LOOP_CONTEXT.value,
            "path": "/iteration_no",
        },
        "right": {"kind": BindingKind.LITERAL.value, "value": 2},
    }
    plan = _while_plan(workflow_id, tool_version_id, predicate=pred)
    plan["steps"].append(_tool("top", tool_version_id=tool_version_id))
    plan["limits"]["max_parallelism"] = 2
    plan["completion"]["response_step_ids"] = ["loop1", "top"]
    return plan


# ---------------------------------------------------------------------------
# Plant helpers
# ---------------------------------------------------------------------------


async def _seed_foreach(
    session: AsyncSession,
    *,
    items: list[Any] | None = None,
    on_error: str = "FAIL_EXECUTION",
    max_attempts: int = 2,
    risk_class: str = RiskClass.READ_ONLY.value,
    timeout_seconds: int = 30,
) -> tuple[uuid.UUID, dict[str, Any], datetime, str, uuid.UUID]:
    items = items if items is not None else [{"id": 1}, {"id": 2}]

    def builder(wid: uuid.UUID, tv: uuid.UUID) -> dict[str, Any]:
        return _foreach_plan(
            wid, tv, on_error=on_error, timeout_seconds=timeout_seconds
        )

    return await _seed_workflow_execution(
        session,
        plan_builder=builder,
        max_attempts=max_attempts,
        risk_class=risk_class,
        inputs={"items": items},
    )


async def _seed_while(
    session: AsyncSession,
    *,
    predicate: dict[str, Any] | None = None,
    on_error: str = "FAIL_EXECUTION",
    max_attempts: int = 2,
    risk_class: str = RiskClass.READ_ONLY.value,
    max_iterations: int = 10,
    timeout_seconds: int = 30,
) -> tuple[uuid.UUID, dict[str, Any], datetime, str, uuid.UUID]:
    def builder(wid: uuid.UUID, tv: uuid.UUID) -> dict[str, Any]:
        return _while_plan(
            wid,
            tv,
            predicate=predicate,
            on_error=on_error,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
        )

    return await _seed_workflow_execution(
        session,
        plan_builder=builder,
        max_attempts=max_attempts,
        risk_class=risk_class,
    )


def _loop_parent(steps: list[Any], key: str = "loop1") -> Any:
    return next(s for s in steps if s.step_key == key)


def _child_key(parent_id: uuid.UUID, iteration_no: int, template: str = "body") -> str:
    return iteration_step_key(
        parent_step_id=parent_id,
        iteration_no=iteration_no,
        template_step_id=template,
    )


async def _start_foreach_running_pinned(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    now: datetime,
) -> Any:
    """RUNNING FOR_EACH parent with collection pin; no children yet."""
    repo = ExecutionRepository(session)
    execution = await repo.get(execution_id)
    assert execution is not None
    plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
    steps = await repo.list_steps(execution_id)
    parent = _loop_parent(steps)
    cfg = LoopStepConfigV1.model_validate(
        ExecutionPlanStep.model_validate(parent.step_snapshot).config
    )
    collection = resolve_foreach_collection(
        execution=execution,
        loop_step=parent,
        plan=plan,
        steps=steps,
        cfg=cfg,
    )
    pin_collection_evidence(loop_step=parent, collection=collection)
    parent.status = StepStatus.RUNNING.value
    if parent.started_at is None:
        parent.started_at = now
    parent.lock_version += 1
    await session.commit()
    return parent


async def _materialize_foreach_iters(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    through: int,
    terminal_through: int = 0,
    now: datetime | None = None,
) -> list[Any]:
    """Materialize iterations 1..through; mark 1..terminal_through SUCCEEDED."""
    ts = now or datetime.now(UTC)
    repo = ExecutionRepository(session)
    execution = await repo.get(execution_id)
    assert execution is not None
    plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
    steps = await repo.list_steps(execution_id)
    parent = _loop_parent(steps)
    cfg = LoopStepConfigV1.model_validate(
        ExecutionPlanStep.model_validate(parent.step_snapshot).config
    )
    created_all: list[Any] = []
    for n in range(1, through + 1):
        created = await materialize_iteration(
            executions=repo,
            execution=execution,
            loop_step=parent,
            plan=plan,
            cfg=cfg,
            iteration_no=n,
            existing_steps=await repo.list_steps(execution_id),
        )
        created_all.extend(created)
        if n <= terminal_through:
            for child in created:
                child.status = StepStatus.SUCCEEDED.value
                child.started_at = ts
                child.finished_at = ts
                child.result_inline = {"ok": True, "iter": n}
                child.lock_version += 1
    await session.commit()
    return created_all


async def _append_while_gate(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    candidate: int,
    result: bool,
) -> None:
    repo = ExecutionRepository(session)
    execution = await repo.get(execution_id)
    assert execution is not None
    plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
    steps = await repo.list_steps(execution_id)
    parent = _loop_parent(steps)
    cfg = LoopStepConfigV1.model_validate(
        ExecutionPlanStep.model_validate(parent.step_snapshot).config
    )
    if parent.resolved_input is None:
        pin_while_control_evidence(loop_step=parent)
    evidence = build_while_evidence_object(
        plan=plan,
        execution=execution,
        loop_step=parent,
        cfg=cfg,
        steps=steps,
        candidate_iteration_no=candidate,
    )
    digest = hash_while_predicate_evidence(evidence)
    parent.resolved_input = append_or_replay_while_history(
        resolved_input=dict(parent.resolved_input),
        next_iteration_no=candidate,
        evidence_hash=digest,
        result=result,
    )
    parent.status = StepStatus.RUNNING.value
    if parent.started_at is None:
        parent.started_at = datetime.now(UTC)
    parent.lock_version += 1
    await session.commit()


async def _materialize_while_iters(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    through: int,
    terminal_through: int = 0,
    now: datetime | None = None,
) -> None:
    ts = now or datetime.now(UTC)
    repo = ExecutionRepository(session)
    execution = await repo.get(execution_id)
    assert execution is not None
    plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
    parent = _loop_parent(await repo.list_steps(execution_id))
    cfg = LoopStepConfigV1.model_validate(
        ExecutionPlanStep.model_validate(parent.step_snapshot).config
    )
    for n in range(1, through + 1):
        created = await materialize_iteration(
            executions=repo,
            execution=execution,
            loop_step=parent,
            plan=plan,
            cfg=cfg,
            iteration_no=n,
            existing_steps=await repo.list_steps(execution_id),
        )
        if n <= terminal_through:
            for child in created:
                child.status = StepStatus.SUCCEEDED.value
                child.started_at = ts
                child.finished_at = ts
                child.result_inline = {
                    "structured_content": {"value": n, "ok": True}
                }
                child.lock_version += 1
    await session.commit()


async def _recover(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    worker_id: str = "rec-worker",
    now: datetime | None = None,
):
    return await ExecutionRecoveryService(session, lease_seconds=120).recover(
        execution_id=execution_id,
        worker_id=worker_id,
        now=now or datetime.now(UTC),
    )


async def _recover_and_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    client: Any,
    worker_id: str = "rec-worker",
    now: datetime | None = None,
):
    ts = now or datetime.now(UTC)
    async with session_factory() as session:
        outcome = await _recover(
            session, execution_id=execution_id, worker_id=worker_id, now=ts
        )
        await session.commit()
    if outcome.invoke_runner and outcome.lease_token is not None:
        runner = _runner(session_factory, client)
        run = await runner.run_claimed_execution(
            execution_id=execution_id,
            worker_id=outcome.worker_id or worker_id,
            lease_token=outcome.lease_token,
        )
        return outcome, run
    return outcome, None


# ---------------------------------------------------------------------------
# Discovery (1–7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_foreach_while_nested_starvation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    fe_id, _c, now, _w, _t = await _seed_foreach(db_session, items=[{"id": 1}])
    await _expire_lease(db_session, execution_id=fe_id, claim_now=now)
    wh_id, _c2, now2, _w2, _t2 = await _seed_while(db_session)
    await _expire_lease(db_session, execution_id=wh_id, claim_now=now2)

    template = await ExecutionRepository(db_session).get(fe_id)
    assert template is not None
    from tests.unit.test_workflow_dag_recovery import _plant_expired_unsupported_workflow

    nested_ids = []
    for i in range(12):
        nested_ids.append(
            await _plant_expired_unsupported_workflow(
                db_session,
                template=template,
                plan_snapshot=_nested_loop_plan(
                    uuid.uuid4(), _c["tool_version_id"]
                ),
                lease_expires_at=now - timedelta(seconds=100 - i),
            )
        )
    await db_session.commit()

    assert plan_supports_workflow_running_recovery(
        (await ExecutionRepository(db_session).get(fe_id)).plan_snapshot
    )
    assert plan_supports_workflow_running_recovery(
        (await ExecutionRepository(db_session).get(wh_id)).plan_snapshot
    )

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=10, now=now)
    assert fe_id in ids
    assert wh_id in ids
    for nid in nested_ids:
        assert nid not in ids


# ---------------------------------------------------------------------------
# FOR_EACH checkpoints (8–21)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreach_zero_child_checkpoint_materializes_iter1(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_foreach(
            session, items=[{"id": 1}, {"id": 2}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client, now=now + timedelta(seconds=2)
    )
    assert outcome.decision in {
        RecoveryDecision.TAKEOVER_READY,
        RecoveryDecision.SAFE_RETRY,
        RecoveryDecision.RESUME_ATTEMPT,
    }
    assert outcome.invoke_runner is True
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        children = [s for s in steps if s.parent_step_id == parent.id]
        assert {c.iteration_no for c in children} >= {1}
        assert len(client.calls) >= 1


@pytest.mark.asyncio
async def test_foreach_next_iteration_checkpoint(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_foreach(
            session, items=[{"id": 1}, {"id": 2}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=1, now=now
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        iters = {s.iteration_no for s in steps if s.parent_step_id == parent.id}
        assert 2 in iters
        assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_foreach_final_iteration_parent_terminalizes_mcp0(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_foreach(
            session, items=[{"id": 1}, {"id": 2}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=2, terminal_through=2, now=now
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _NeverClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status in {
            ExecutionStatus.SUCCEEDED.value,
            ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        }
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        assert parent.status == StepStatus.SUCCEEDED.value
    assert len(getattr(client, "calls", [])) == 0


@pytest.mark.asyncio
async def test_foreach_active_ready_resume(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, old_w, tok = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        child = next(
            s
            for s in steps
            if s.step_key == _child_key(parent.id, 1)
        )
        child.status = StepStatus.READY.value
        child.ready_at = now
        child.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)
        del old_w, tok

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.decision == RecoveryDecision.TAKEOVER_READY
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_foreach_started_attempt_resume_same_attempt(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, old_w, tok = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        child_key = _child_key(parent.id, 1)
        attempt_id = await _start_attempt_no_toolcall(
            session,
            execution_id=eid,
            step_key=child_key,
            worker_id=old_w,
            lease_token=tok,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client, worker_id="new-w"
    )
    assert outcome.decision == RecoveryDecision.RESUME_ATTEMPT
    assert len(client.calls) == 1
    async with db_session_factory() as session:
        attempts = await ExecutionRepository(session).list_attempts(
            next(
                s.id
                for s in await ExecutionRepository(session).list_steps(eid)
                if s.step_key == child_key
            )
        )
        assert len(attempts) == 1
        assert attempts[0].id == attempt_id
        assert attempts[0].status == StepAttemptStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_foreach_safe_started_toolcall_retry_once(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, old_w, _tok = await _seed_foreach(
            session, items=[{"id": 1}], max_attempts=2
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        child_key = _child_key(parent.id, 1)
        await _plant_started_toolcall(
            session,
            execution_id=eid,
            step_key=child_key,
            worker_id=old_w,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.decision == RecoveryDecision.SAFE_RETRY
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_foreach_unsafe_started_toolcall_unknown_mcp0(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, old_w, _tok = await _seed_foreach(
            session, items=[{"id": 1}], max_attempts=2
        )
        await _pin_all_tool_policies(
            session,
            execution_id=eid,
            risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
            max_attempts=2,
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        await _plant_started_toolcall(
            session,
            execution_id=eid,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.UNKNOWN_OUTCOME
    assert outcome.invoke_runner is False
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        steps = await ExecutionRepository(session).list_steps(eid)
        child = next(s for s in steps if s.parent_step_id is not None)
        assert child.status == StepStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
async def test_foreach_retry_exhausted_fail_execution(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, old_w, _tok = await _seed_foreach(
            session,
            items=[{"id": 1}, {"id": 2}],
            on_error="FAIL_EXECUTION",
            max_attempts=1,
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        await _plant_started_toolcall(
            session,
            execution_id=eid,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_EXHAUSTED
    assert outcome.invoke_runner is False
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value


@pytest.mark.asyncio
async def test_foreach_retry_exhausted_continue_and_mark_partial(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    # CONTINUE
    async with db_session_factory() as session:
        eid, _c, now, old_w, _tok = await _seed_foreach(
            session,
            items=[{"id": 1}, {"id": 2}],
            on_error="CONTINUE",
            max_attempts=1,
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        await _plant_started_toolcall(
            session,
            execution_id=eid,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    # iter2 body MCP after CONTINUE failure of iter1
    assert len(client.calls) >= 1

    # MARK_PARTIAL
    async with db_session_factory() as session:
        eid2, _c2, now2, old_w2, _tok2 = await _seed_foreach(
            session,
            items=[{"id": 1}],
            on_error="MARK_PARTIAL",
            max_attempts=1,
        )
        await _start_foreach_running_pinned(session, execution_id=eid2, now=now2)
        await _materialize_foreach_iters(
            session, execution_id=eid2, through=1, terminal_through=0, now=now2
        )
        steps = await ExecutionRepository(session).list_steps(eid2)
        parent = _loop_parent(steps)
        await _plant_started_toolcall(
            session,
            execution_id=eid2,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w2,
            now=now2,
        )
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    client2 = _NeverClient()
    outcome2, _run2 = await _recover_and_run(
        db_session_factory, execution_id=eid2, client=client2
    )
    assert outcome2.taken_over is True
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid2)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value


@pytest.mark.asyncio
async def test_foreach_pin_drift_and_missing_pin_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        parent.resolved_input = {
            "mode": "FOR_EACH",
            "collection_hash": "a" * 64,
            "collection_size": 1,
        }
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert outcome.invoke_runner is False

    async with db_session_factory() as session:
        eid2, _c2, now2, _w2, _tok2 = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid2))
        parent.status = StepStatus.RUNNING.value
        parent.started_at = now2
        parent.resolved_input = None
        parent.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    async with db_session_factory() as session:
        outcome2 = await _recover(session, execution_id=eid2)
        await session.commit()
    assert outcome2.decision == RecoveryDecision.FAIL_INCONSISTENT


def _foreach_two_body_plan(
    workflow_id: uuid.UUID, tool_version_id: uuid.UUID
) -> dict[str, Any]:
    plan = _foreach_plan(workflow_id, tool_version_id)
    # Add CONDITION body so partial materialization (1 of 2) is possible.
    plan["steps"][0]["config"]["body_step_ids"] = ["body", "cond"]
    plan["steps"].append(
        {
            "id": "cond",
            "name": "cond",
            "type": AuthorableStepType.CONDITION.value,
            "required": True,
            "depends_on": ["loop1", "body"],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "predicate": {
                    "op": "eq",
                    "left": {"kind": BindingKind.LITERAL.value, "value": True},
                    "right": {"kind": BindingKind.LITERAL.value, "value": True},
                }
            },
        }
    )
    return plan


@pytest.mark.asyncio
async def test_foreach_partial_gap_two_active_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    # Partial materialization: only TOOL body row, missing CONDITION body.
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_workflow_execution(
            session,
            plan_builder=_foreach_two_body_plan,
            inputs={"items": [{"id": 1}]},
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        repo = ExecutionRepository(session)
        execution = await repo.get(eid)
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        parent = _loop_parent(await repo.list_steps(eid))
        template = next(ps for ps in plan.steps if ps.id == "body")
        await repo.create_step(
            execution_id=eid,
            step_key=_child_key(parent.id, 1, "body"),
            step_type=AuthorableStepType.TOOL.value,
            mcp_tool_version_id=tool_version_for_template(template),
            parent_step_id=parent.id,
            sequence_hint=1,
            status=StepStatus.PENDING.value,
            step_snapshot=template.model_dump(mode="json"),
            lock_version=1,
            iteration_no=1,
        )
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT

    # Gap 1,3
    async with db_session_factory() as session:
        eid2, _c2, now2, _w2, _tok2 = await _seed_foreach(
            session, items=[{"id": 1}, {"id": 2}, {"id": 3}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid2, now=now2)
        await _materialize_foreach_iters(
            session, execution_id=eid2, through=1, terminal_through=1, now=now2
        )
        # Force-create iteration 3 without 2
        repo = ExecutionRepository(session)
        execution = await repo.get(eid2)
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        parent = _loop_parent(await repo.list_steps(eid2))
        cfg = LoopStepConfigV1.model_validate(
            ExecutionPlanStep.model_validate(parent.step_snapshot).config
        )
        template = next(ps for ps in plan.steps if ps.id == "body")
        await repo.create_step(
            execution_id=eid2,
            step_key=_child_key(parent.id, 3),
            step_type=AuthorableStepType.TOOL.value,
            mcp_tool_version_id=tool_version_for_template(template),
            parent_step_id=parent.id,
            sequence_hint=99,
            status=StepStatus.PENDING.value,
            step_snapshot=template.model_dump(mode="json"),
            lock_version=1,
            iteration_no=3,
        )
        await session.commit()
        await _expire_lease(session, execution_id=eid2, claim_now=now2)
        del cfg

    async with db_session_factory() as session:
        outcome2 = await _recover(session, execution_id=eid2)
        await session.commit()
    assert outcome2.decision == RecoveryDecision.FAIL_INCONSISTENT

    # Two active iterations
    async with db_session_factory() as session:
        eid3, _c3, now3, _w3, _tok3 = await _seed_foreach(
            session, items=[{"id": 1}, {"id": 2}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid3, now=now3)
        await _materialize_foreach_iters(
            session, execution_id=eid3, through=2, terminal_through=0, now=now3
        )
        await _expire_lease(session, execution_id=eid3, claim_now=now3)

    async with db_session_factory() as session:
        outcome3 = await _recover(session, execution_id=eid3)
        await session.commit()
    assert outcome3.decision == RecoveryDecision.FAIL_INCONSISTENT


# ---------------------------------------------------------------------------
# WHILE checkpoints (22–34)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_while_empty_history_zero_children_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired RUNNING WHILE + empty predicate_history + 0 children is corrupt."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_while(session)
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        pin_while_control_evidence(loop_step=parent)
        parent.status = StepStatus.RUNNING.value
        parent.started_at = now
        parent.lock_version += 1
        await session.commit()
        assert parent.resolved_input == {
            "mode": "WHILE",
            "predicate_history": [],
        }
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert outcome.invoke_runner is False
    assert outcome.taken_over is False
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "RECOVERY_INCONSISTENT_EVIDENCE"
        children = [
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.parent_step_id is not None
        ]
        assert children == []


@pytest.mark.asyncio
async def test_while_true_gate_zero_children_materializes_once(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    # Checkpoint A: gate1=true + 0 children → materialize once.
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_while(
            session,
            predicate={
                "op": "eq",
                "left": {"kind": BindingKind.LITERAL.value, "value": True},
                "right": {"kind": BindingKind.LITERAL.value, "value": True},
            },
            max_iterations=1,
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=1, result=True
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        assert any(s.parent_step_id == parent.id and s.iteration_no == 1 for s in steps)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_while_false_gate_zero_children_parent_succeeds_mcp0(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkpoint B: history 1=false + 0 children → parent SUCCEEDED, MCP 0."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_while(
            session,
            predicate={
                "op": "eq",
                "left": {"kind": BindingKind.LITERAL.value, "value": False},
                "right": {"kind": BindingKind.LITERAL.value, "value": True},
            },
            max_iterations=5,
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=1, result=False
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _NeverClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    async with db_session_factory() as session:
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        assert parent.status == StepStatus.SUCCEEDED.value
        assert parent.result_inline == {
            "mode": "WHILE",
            "iterations_completed": 0,
        }
        children = [
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.parent_step_id == parent.id
        ]
        assert children == []
    assert len(getattr(client, "calls", [])) == 0


@pytest.mark.asyncio
async def test_while_next_predicate_and_true_gate_no_rows(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    # B: iter1 complete, history ends at 1=true → evaluate candidate 2
    false_after = {
        "op": "lt",
        "left": {
            "kind": BindingKind.LOOP_CONTEXT.value,
            "path": "/iteration_no",
        },
        "right": {"kind": BindingKind.LITERAL.value, "value": 2},
    }
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_while(
            session, predicate=false_after, max_iterations=5
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid, through=1, terminal_through=1, now=now
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _NeverClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    async with db_session_factory() as session:
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        hist = parse_while_predicate_history(parent.resolved_input)
        assert any(
            e["next_iteration_no"] == 2 and e["result"] is False
            for e in hist["predicate_history"]
        )
        assert parent.status == StepStatus.SUCCEEDED.value

    # C: history candidate2=true, no iter2 rows
    async with db_session_factory() as session:
        eid2, _c2, now2, _w2, _tok2 = await _seed_while(
            session,
            predicate={
                "op": "eq",
                "left": {"kind": BindingKind.LITERAL.value, "value": True},
                "right": {"kind": BindingKind.LITERAL.value, "value": True},
            },
            max_iterations=2,
        )
        await _append_while_gate(
            session, execution_id=eid2, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid2, through=1, terminal_through=1, now=now2
        )
        await _append_while_gate(
            session, execution_id=eid2, candidate=2, result=True
        )
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    client2 = _OkClient()
    outcome2, _run2 = await _recover_and_run(
        db_session_factory, execution_id=eid2, client=client2
    )
    assert outcome2.taken_over is True
    assert len(client2.calls) == 1


@pytest.mark.asyncio
async def test_while_false_gate_parent_succeeds_mcp0(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    pred = {
        "op": "lt",
        "left": {
            "kind": BindingKind.LOOP_CONTEXT.value,
            "path": "/iteration_no",
        },
        "right": {"kind": BindingKind.LITERAL.value, "value": 2},
    }
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_while(
            session, predicate=pred, max_iterations=5
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid, through=1, terminal_through=1, now=now
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=2, result=False
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _NeverClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    async with db_session_factory() as session:
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        assert parent.status == StepStatus.SUCCEEDED.value
        assert parent.result_inline == {
            "mode": "WHILE",
            "iterations_completed": 1,
        }
        children = [
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.parent_step_id == parent.id
        ]
        assert len(children) == 1


@pytest.mark.asyncio
async def test_while_resume_safe_retry_unsafe(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    # Resume attempt
    async with db_session_factory() as session:
        eid, _c, now, old_w, tok = await _seed_while(
            session, max_iterations=1
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        child_key = _child_key(parent.id, 1)
        attempt_id = await _start_attempt_no_toolcall(
            session,
            execution_id=eid,
            step_key=child_key,
            worker_id=old_w,
            lease_token=tok,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client, worker_id="nw"
    )
    assert outcome.decision == RecoveryDecision.RESUME_ATTEMPT
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(eid)
        child = next(s for s in steps if s.step_key == child_key)
        attempts = await ExecutionRepository(session).list_attempts(child.id)
        assert attempts[0].id == attempt_id
        assert attempts[0].worker_id == "nw"

    # Safe retry
    async with db_session_factory() as session:
        eid2, _c2, now2, old_w2, _tok2 = await _seed_while(
            session, max_iterations=1, max_attempts=2
        )
        await _append_while_gate(
            session, execution_id=eid2, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid2, through=1, terminal_through=0, now=now2
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid2))
        await _plant_started_toolcall(
            session,
            execution_id=eid2,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w2,
            now=now2,
        )
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    client2 = _OkClient()
    outcome2, _run2 = await _recover_and_run(
        db_session_factory, execution_id=eid2, client=client2
    )
    assert outcome2.decision == RecoveryDecision.SAFE_RETRY
    assert len(client2.calls) == 1

    # Unsafe
    async with db_session_factory() as session:
        eid3, _c3, now3, old_w3, _tok3 = await _seed_while(
            session, max_iterations=1, max_attempts=2
        )
        await _pin_all_tool_policies(
            session,
            execution_id=eid3,
            risk_class=RiskClass.DESTRUCTIVE.value,
            max_attempts=2,
        )
        await _append_while_gate(
            session, execution_id=eid3, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid3, through=1, terminal_through=0, now=now3
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid3))
        await _plant_started_toolcall(
            session,
            execution_id=eid3,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w3,
            now=now3,
        )
        await _expire_lease(session, execution_id=eid3, claim_now=now3)

    async with db_session_factory() as session:
        outcome3 = await _recover(session, execution_id=eid3)
        await session.commit()
    assert outcome3.decision == RecoveryDecision.UNKNOWN_OUTCOME
    assert outcome3.invoke_runner is False


@pytest.mark.asyncio
async def test_while_history_corruption_and_hash_drift(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    # Gap / malformed
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_while(session)
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        parent.status = StepStatus.RUNNING.value
        parent.started_at = now
        parent.resolved_input = {
            "mode": "WHILE",
            "predicate_history": [
                {
                    "next_iteration_no": 2,
                    "evidence_hash": "a" * 64,
                    "result": True,
                }
            ],
        }
        parent.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT

    # Materialized without true gate
    async with db_session_factory() as session:
        eid2, _c2, now2, _w2, _tok2 = await _seed_while(session)
        await _append_while_gate(
            session, execution_id=eid2, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid2, through=1, terminal_through=0, now=now2
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid2))
        parent.resolved_input = {"mode": "WHILE", "predicate_history": []}
        await session.commit()
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    async with db_session_factory() as session:
        outcome2 = await _recover(session, execution_id=eid2)
        await session.commit()
    assert outcome2.decision == RecoveryDecision.FAIL_INCONSISTENT

    # Rows after false gate
    async with db_session_factory() as session:
        eid3, _c3, now3, _w3, _tok3 = await _seed_while(session)
        await _append_while_gate(
            session, execution_id=eid3, candidate=1, result=False
        )
        # Force child despite false gate
        repo = ExecutionRepository(session)
        execution = await repo.get(eid3)
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        parent = _loop_parent(await repo.list_steps(eid3))
        template = next(ps for ps in plan.steps if ps.id == "body")
        await repo.create_step(
            execution_id=eid3,
            step_key=_child_key(parent.id, 1),
            step_type=AuthorableStepType.TOOL.value,
            mcp_tool_version_id=tool_version_for_template(template),
            parent_step_id=parent.id,
            sequence_hint=1,
            status=StepStatus.PENDING.value,
            step_snapshot=template.model_dump(mode="json"),
            lock_version=1,
            iteration_no=1,
        )
        await session.commit()
        await _expire_lease(session, execution_id=eid3, claim_now=now3)

    async with db_session_factory() as session:
        outcome3 = await _recover(session, execution_id=eid3)
        await session.commit()
    assert outcome3.decision == RecoveryDecision.FAIL_INCONSISTENT

    # evidence_hash drift
    async with db_session_factory() as session:
        eid4, _c4, now4, _w4, _tok4 = await _seed_while(session)
        await _append_while_gate(
            session, execution_id=eid4, candidate=1, result=True
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid4))
        hist = parse_while_predicate_history(parent.resolved_input)
        bad = dict(parent.resolved_input)
        entries = list(hist["predicate_history"])
        entries[0] = {**entries[0], "evidence_hash": "f" * 64}
        bad["predicate_history"] = entries
        parent.resolved_input = bad
        await session.commit()
        await _expire_lease(session, execution_id=eid4, claim_now=now4)

    async with db_session_factory() as session:
        outcome4 = await _recover(session, execution_id=eid4)
        await session.commit()
    assert outcome4.decision == RecoveryDecision.FAIL_INCONSISTENT


@pytest.mark.asyncio
async def test_while_max_iterations_and_timeout_started_at(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    # max_iterations boundary: true gate at max+1 with complete iters
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_while(
            session,
            predicate={
                "op": "eq",
                "left": {"kind": BindingKind.LITERAL.value, "value": True},
                "right": {"kind": BindingKind.LITERAL.value, "value": True},
            },
            max_iterations=1,
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=1, result=True
        )
        await _materialize_while_iters(
            session, execution_id=eid, through=1, terminal_through=1, now=now
        )
        await _append_while_gate(
            session, execution_id=eid, candidate=2, result=True
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _NeverClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    async with db_session_factory() as session:
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        assert parent.status == StepStatus.FAILED.value
        from app.execution.loop_runtime import LOOP_MAX_ITERATIONS_EXCEEDED

        assert parent.error_code == LOOP_MAX_ITERATIONS_EXCEEDED
        children = [
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.parent_step_id == parent.id
        ]
        assert max(c.iteration_no for c in children) == 1

    # timeout: started_at preserved, LOOP_TIMEOUT
    async with db_session_factory() as session:
        eid2, _c2, now2, _w2, _tok2 = await _seed_while(
            session, timeout_seconds=1, max_iterations=5
        )
        await _append_while_gate(
            session, execution_id=eid2, candidate=1, result=True
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid2))
        started = now2 - timedelta(seconds=10)
        parent.started_at = started
        await session.commit()
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    client2 = _NeverClient()
    outcome2, _run2 = await _recover_and_run(
        db_session_factory, execution_id=eid2, client=client2
    )
    assert outcome2.taken_over is True
    async with db_session_factory() as session:
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid2))
        assert parent.started_at is not None
        # Recovery must not reset LOOP started_at (tz-naive DB roundtrip ok).
        assert parent.started_at.replace(tzinfo=UTC) == started.replace(tzinfo=UTC)
        from app.execution.loop_runtime import LOOP_TIMEOUT

        assert parent.status == StepStatus.TIMED_OUT.value
        assert parent.error_code == LOOP_TIMEOUT


# ---------------------------------------------------------------------------
# Lineage / parallel / races (35–46)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_foreach_missing_children_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A: SUCCEEDED FOR_EACH collection_size>0 with zero children fail-closes."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_workflow_execution(
            session,
            plan_builder=_loop_plus_top_tool_plan,
            inputs={"items": [{"id": 1}, {"id": 2}]},
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        pin = parse_foreach_collection_pin(parent.resolved_input)
        assert pin["collection_size"] == 2
        succeed_loop(
            parent,
            collection_size=2,
            iterations_completed=2,
            now=now,
        )
        top = next(
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.step_key == "top"
        )
        top.status = StepStatus.READY.value
        top.ready_at = now
        top.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert outcome.taken_over is False
    assert outcome.invoke_runner is False
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None


@pytest.mark.asyncio
async def test_terminal_foreach_result_inline_tamper_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B: completed FOR_EACH children + tampered result_inline fail-closes."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_workflow_execution(
            session,
            plan_builder=_loop_plus_top_tool_plan,
            inputs={"items": [{"id": 1}, {"id": 2}]},
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=2, terminal_through=2, now=now
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        succeed_loop(
            parent,
            collection_size=2,
            iterations_completed=2,
            now=now,
        )
        parent.result_inline = {
            "mode": "FOR_EACH",
            "iterations_completed": 1,
            "collection_size": 2,
        }
        parent.lock_version += 1
        top = next(
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.step_key == "top"
        )
        top.status = StepStatus.READY.value
        top.ready_at = now
        top.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert outcome.taken_over is False
    assert outcome.invoke_runner is False


@pytest.mark.asyncio
async def test_terminal_foreach_zero_iteration_valid_checkpoint(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C: collection_size=0 + children=0 + exact result_inline remains valid."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_workflow_execution(
            session,
            plan_builder=_loop_plus_top_tool_plan,
            inputs={"items": []},
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        pin = parse_foreach_collection_pin(parent.resolved_input)
        assert pin["collection_size"] == 0
        succeed_loop(
            parent,
            collection_size=0,
            iterations_completed=0,
            now=now,
        )
        top = next(
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.step_key == "top"
        )
        top.status = StepStatus.READY.value
        top.ready_at = now
        top.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    assert outcome.invoke_runner is True
    assert len(client.calls) == 1
    async with db_session_factory() as session:
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        assert parent.status == StepStatus.SUCCEEDED.value
        assert parent.result_inline == {
            "mode": "FOR_EACH",
            "iterations_completed": 0,
            "collection_size": 0,
        }
        children = [
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.parent_step_id == parent.id
        ]
        assert children == []


async def _plant_terminal_succeeded_while_with_child(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    now: datetime,
) -> Any:
    """SUCCEEDED WHILE with one SUCCEEDED body child + false gate at candidate 2."""
    await _append_while_gate(
        session, execution_id=execution_id, candidate=1, result=True
    )
    await _materialize_while_iters(
        session, execution_id=execution_id, through=1, terminal_through=1, now=now
    )
    await _append_while_gate(
        session, execution_id=execution_id, candidate=2, result=False
    )
    parent = _loop_parent(await ExecutionRepository(session).list_steps(execution_id))
    succeed_while_loop(parent, iterations_completed=1, now=now)
    await session.commit()
    return parent


@pytest.mark.asyncio
async def test_terminal_while_child_lineage_tamper_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A: terminal SUCCEEDED WHILE + tampered child lineage blocks whole recovery."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_workflow_execution(
            session,
            plan_builder=_while_plus_top_tool_plan,
        )
        await _plant_terminal_succeeded_while_with_child(
            session, execution_id=eid, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        child = next(s for s in steps if s.parent_step_id == parent.id)
        child.step_key = "tampered-while-child-key"
        # Top-level TOOL remains READY-eligible so recovery would otherwise proceed.
        top = next(s for s in steps if s.step_key == "top")
        top.status = StepStatus.READY.value
        top.ready_at = now
        top.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert outcome.taken_over is False
    assert outcome.invoke_runner is False
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None


@pytest.mark.asyncio
async def test_terminal_while_history_result_inline_tamper_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B: terminal SUCCEEDED WHILE history/result_inline drift fail-closes."""
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_workflow_execution(
            session,
            plan_builder=_while_plus_top_tool_plan,
        )
        await _plant_terminal_succeeded_while_with_child(
            session, execution_id=eid, now=now
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid))
        # Corrupt result_inline while history still says iterations_completed=1.
        parent.result_inline = {
            "mode": "WHILE",
            "iterations_completed": 99,
        }
        parent.lock_version += 1
        top = next(
            s
            for s in await ExecutionRepository(session).list_steps(eid)
            if s.step_key == "top"
        )
        top.status = StepStatus.READY.value
        top.ready_at = now
        top.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert outcome.taken_over is False
    assert outcome.invoke_runner is False
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.lease_token is None

    # Also history tamper (remove final false gate) with valid result_inline.
    async with db_session_factory() as session:
        eid2, _c2, now2, _w2, _tok2 = await _seed_workflow_execution(
            session,
            plan_builder=_while_plus_top_tool_plan,
        )
        await _plant_terminal_succeeded_while_with_child(
            session, execution_id=eid2, now=now2
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid2))
        hist = parse_while_predicate_history(parent.resolved_input)
        parent.resolved_input = {
            "mode": "WHILE",
            "predicate_history": hist["predicate_history"][:1],
        }
        parent.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    async with db_session_factory() as session:
        outcome2 = await _recover(session, execution_id=eid2)
        await session.commit()
    assert outcome2.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert outcome2.taken_over is False
    assert outcome2.invoke_runner is False


@pytest.mark.asyncio
async def test_child_lineage_tamper_and_unknown_and_terminal_parent(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        child = next(s for s in steps if s.parent_step_id == parent.id)
        child.step_key = "tampered-key"
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT

    # UNKNOWN child never READY
    async with db_session_factory() as session:
        eid2, _c2, now2, _w2, _tok2 = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid2, now=now2)
        await _materialize_foreach_iters(
            session, execution_id=eid2, through=1, terminal_through=0, now=now2
        )
        steps = await ExecutionRepository(session).list_steps(eid2)
        child = next(s for s in steps if s.parent_step_id is not None)
        child.status = StepStatus.UNKNOWN_OUTCOME.value
        child.finished_at = now2
        child.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid2, claim_now=now2)

    async with db_session_factory() as session:
        outcome2 = await _recover(session, execution_id=eid2)
        await session.commit()
        child = next(
            s
            for s in await ExecutionRepository(session).list_steps(eid2)
            if s.parent_step_id is not None
        )
        assert child.status == StepStatus.UNKNOWN_OUTCOME.value
    assert outcome2.decision == RecoveryDecision.UNKNOWN_OUTCOME

    # Terminal parent + active child
    async with db_session_factory() as session:
        eid3, _c3, now3, _w3, _tok3 = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid3, now=now3)
        await _materialize_foreach_iters(
            session, execution_id=eid3, through=1, terminal_through=0, now=now3
        )
        parent = _loop_parent(await ExecutionRepository(session).list_steps(eid3))
        parent.status = StepStatus.SUCCEEDED.value
        parent.finished_at = now3
        parent.result_inline = {
            "mode": "FOR_EACH",
            "iterations_completed": 1,
            "collection_size": 1,
        }
        parent.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid3, claim_now=now3)

    async with db_session_factory() as session:
        outcome3 = await _recover(session, execution_id=eid3)
        await session.commit()
    assert outcome3.decision == RecoveryDecision.FAIL_INCONSISTENT


@pytest.mark.asyncio
async def test_safe_loop_child_unsafe_top_level_blocks_all_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, old_w, _tok = await _seed_workflow_execution(
            session,
            plan_builder=_loop_plus_top_tool_plan,
            inputs={"items": [{"id": 1}]},
            max_attempts=2,
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _materialize_foreach_iters(
            session, execution_id=eid, through=1, terminal_through=0, now=now
        )
        steps = await ExecutionRepository(session).list_steps(eid)
        parent = _loop_parent(steps)
        # Safe LOOP child STARTED ToolCall
        await _plant_started_toolcall(
            session,
            execution_id=eid,
            step_key=_child_key(parent.id, 1),
            worker_id=old_w,
            now=now,
        )
        # Unsafe top-level sibling
        await _pin_step_risk(
            session,
            execution_id=eid,
            step_key="top",
            risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
        )
        await _plant_started_toolcall(
            session,
            execution_id=eid,
            step_key="top",
            worker_id=old_w,
            now=now,
        )
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as session:
        outcome = await _recover(session, execution_id=eid)
        await session.commit()
    assert outcome.decision == RecoveryDecision.UNKNOWN_OUTCOME
    assert outcome.invoke_runner is False


@pytest.mark.asyncio
async def test_two_independent_loops_one_takeover(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, old_w, tok = await _seed_workflow_execution(
            session,
            plan_builder=_two_loop_plan,
            inputs={"items_a": [{"id": 1}], "items_b": [{"id": 1}]},
        )
        repo = ExecutionRepository(session)
        execution = await repo.get(eid)
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        steps = await repo.list_steps(eid)
        for key in ("loop_a", "loop_b"):
            parent = next(s for s in steps if s.step_key == key)
            cfg = LoopStepConfigV1.model_validate(
                ExecutionPlanStep.model_validate(parent.step_snapshot).config
            )
            collection = resolve_foreach_collection(
                execution=execution,
                loop_step=parent,
                plan=plan,
                steps=steps,
                cfg=cfg,
            )
            pin_collection_evidence(loop_step=parent, collection=collection)
            parent.status = StepStatus.RUNNING.value
            parent.started_at = now
            parent.lock_version += 1
            created = await materialize_iteration(
                executions=repo,
                execution=execution,
                loop_step=parent,
                plan=plan,
                cfg=cfg,
                iteration_no=1,
                existing_steps=await repo.list_steps(eid),
            )
            for child in created:
                child.status = StepStatus.READY.value
                child.ready_at = now
                child.lock_version += 1
        await session.commit()
        await _expire_lease(session, execution_id=eid, claim_now=now)
        del old_w, tok

    client = _OkClient()
    outcome, _run = await _recover_and_run(
        db_session_factory, execution_id=eid, client=client
    )
    assert outcome.taken_over is True
    assert outcome.invoke_runner is True
    assert len(client.calls) == 2
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(eid)
        assert execution is not None
        assert execution.lease_token is not None or execution.status in {
            ExecutionStatus.SUCCEEDED.value,
            ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        }


@pytest.mark.asyncio
async def test_duplicate_recovery_winner_loser_noop(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        eid, _c, now, _w, _tok = await _seed_foreach(
            session, items=[{"id": 1}]
        )
        await _start_foreach_running_pinned(session, execution_id=eid, now=now)
        await _expire_lease(session, execution_id=eid, claim_now=now)

    async with db_session_factory() as s1, db_session_factory() as s2:
        o1 = await _recover(s1, execution_id=eid, worker_id="w1")
        await s1.commit()
        o2 = await _recover(s2, execution_id=eid, worker_id="w2")
        await s2.commit()
    winners = [o for o in (o1, o2) if o.taken_over]
    losers = [o for o in (o1, o2) if not o.taken_over]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].decision == RecoveryDecision.NO_OP


# ---------------------------------------------------------------------------
# Regressions (47–50) — smoke via import of existing suites' helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_loop_and_agent_recovery_regression(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    eid, _c, now, _w, old_tok = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    await _expire_lease(db_session, execution_id=eid, claim_now=now)
    rec = await _recover(db_session, execution_id=eid, worker_id="reg-w")
    await db_session.commit()
    assert rec.decision == RecoveryDecision.TAKEOVER_READY
    assert rec.lease_token != old_tok

    from tests.unit.test_execution_recovery import _claim_ready

    agent_id, _old, agent_tok, claim_now = await _claim_ready(db_session)
    await _expire_lease(db_session, execution_id=agent_id, claim_now=claim_now)
    agent_rec = await _recover(
        db_session, execution_id=agent_id, worker_id="agent-reg"
    )
    await db_session.commit()
    assert agent_rec.decision == RecoveryDecision.TAKEOVER_READY
    assert agent_rec.lease_token != agent_tok
