"""MRTR response / reject / resume / next-round (PR #41)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    McpInputRequestStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.mrtr_reject import MrtrRejectService
from app.execution.mrtr_response import MrtrResponseService
from app.execution.mrtr_resume import MrtrResumeClaimService
from app.execution.queue import (
    OutboxRelayService,
    validate_execution_mrtr_resume_event,
)
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedInputRequired, NormalizedToolResult
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import _install_no_side_effects
from tests.unit.test_mrtr_waiting_input import (
    _CANARY,
    _INPUT_REQUESTS,
    _MrtrClient,
    _assert_canary_absent,
    _runner,
)
from tests.unit.test_tool_runner import _claim_ready_execution, _resolver_factory


class _SequenceClient:
    """First call → input_required; subsequent calls driven by ``results``."""

    def __init__(self, results: list[Any]) -> None:
        self.calls: list[dict] = []
        self._results = list(results)

    async def call_tool(self, endpoint, **kwargs):
        # Copy mutable payloads — runner clears input_responses after send.
        snap = dict(kwargs)
        if isinstance(snap.get("input_responses"), dict):
            snap["input_responses"] = dict(snap["input_responses"])
        if isinstance(snap.get("arguments"), dict):
            snap["arguments"] = dict(snap["arguments"])
        self.calls.append({"endpoint": endpoint, **snap})
        if not self._results:
            raise AssertionError("unexpected extra MCP call")
        result = self._results.pop(0)
        return result, {"http_status": 200, "duration_ms": 3}, datetime.now(UTC)


def _success_result() -> NormalizedToolResult:
    return NormalizedToolResult(
        protocol_success=True,
        tool_error=False,
        content=[{"type": "text", "text": "done"}],
        structured_content=None,
        raw_size_bytes=16,
        duration_ms=3,
    )


def _input_required(
    *,
    request_state: Any = None,
    input_requests: dict[str, Any] | None = None,
) -> NormalizedInputRequired:
    return NormalizedInputRequired(
        input_requests=dict(input_requests or _INPUT_REQUESTS),
        request_state=(
            {"opaque": True, "token": _CANARY}
            if request_state is None
            else request_state
        ),
        raw_size_bytes=64,
        duration_ms=3,
    )


async def _enter_waiting(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    request_state: Any = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (execution_id, input_request_id, requester_id)."""
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token = await _claim_ready_execution(session_factory)
    client = _MrtrClient(request_state=request_state)
    outcome = await _runner(session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.terminal_status == StepStatus.WAITING_INPUT.value
    async with session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        mir = (
            await MCPInputRequestRepository(session).list_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        )[0]
        return execution_id, mir.id, execution.requester_id


@pytest.mark.asyncio
async def test_response_resume_success_exact_request_state(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opaque = {"z": "\t trailing", "n": None, "arr": [1, "x"], "token": _CANARY}
    execution_id, mir_id, actor = await _enter_waiting(
        db_session_factory, monkeypatch, request_state=opaque
    )

    async with db_session_factory() as session:
        outcome = await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
            responses={"city": "Seoul", "units": "c"},
        )
        await session.commit()
        assert outcome.resume_enqueued is True
        assert outcome.status == McpInputRequestStatus.ANSWERED.value

        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_MRTR_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        validated_exec, validated_mir = validate_execution_mrtr_resume_event(events[0])
        assert validated_exec == execution_id
        assert validated_mir == mir_id

    client = _SequenceClient([_success_result()])
    async with db_session_factory() as session:
        claim = await MrtrResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            input_request_id=mir_id,
            worker_id="worker-mrtr",
        )
        await session.commit()
        assert claim.claimed is True
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-mrtr",
        lease_token=lease_token,
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["include_mrtr_resume"] is True
    assert call["input_responses"] == {"city": "Seoul", "units": "c"}
    assert call["request_state"] == opaque
    assert call["request_state"]["token"] == _CANARY

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempt.id)
        assert len(tool_calls) == 2
        assert all(
            tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
            for tc in tool_calls
        )
        # requestState never leaks into ToolCall meta.
        for tc in tool_calls:
            _assert_canary_absent(tc.request_meta)
            _assert_canary_absent(tc.response_meta)


@pytest.mark.asyncio
async def test_response_resume_second_input_required(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, actor = await _enter_waiting(db_session_factory, monkeypatch)
    second_state = {"round": 2, "token": _CANARY + "-r2"}

    async with db_session_factory() as session:
        await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
            responses={"city": "Busan", "units": "f"},
        )
        await session.commit()

    client = _SequenceClient(
        [
            _input_required(
                request_state=second_state,
                input_requests={"confirm": {"schema": {"type": "boolean"}}},
            )
        ]
    )
    async with db_session_factory() as session:
        claim = await MrtrResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            input_request_id=mir_id,
            worker_id="worker-mrtr",
        )
        await session.commit()
        assert claim.claimed
        lease_token = claim.lease_token
        assert lease_token is not None

    outcome = await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id,
        worker_id="worker-mrtr",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.WAITING_INPUT.value
    assert len(client.calls) == 1
    assert client.calls[0]["request_state"]["token"] == _CANARY

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_INPUT.value
        assert execution.worker_id is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        rows = await MCPInputRequestRepository(session).list_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(rows) == 2
        by_round = {r.round_no: r for r in rows}
        assert by_round[1].status == McpInputRequestStatus.ANSWERED.value
        assert by_round[2].status == McpInputRequestStatus.OPEN.value
        assert by_round[2].request_state == second_state
        open_rows = await MCPInputRequestRepository(session).list_open_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(open_rows) == 1


@pytest.mark.asyncio
async def test_reject_zero_mcp_calls(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, actor = await _enter_waiting(db_session_factory, monkeypatch)

    async with db_session_factory() as session:
        outcome = await MrtrRejectService(session).reject(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
        )
        await session.commit()
        assert outcome.status == McpInputRequestStatus.REJECTED.value
        assert outcome.execution_status == ExecutionStatus.FAILED.value

        mir = await MCPInputRequestRepository(session).get(mir_id)
        assert mir is not None
        assert mir.response_payload is None
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.error_code == "MRTR_REJECTED"
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        assert attempt.status == StepAttemptStatus.FAILED.value

        # Reject creates no MRTR resume outbox.
        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_MRTR_RESUME"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert events == []

        # Cannot resume rejected request.
        claim = await MrtrResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            input_request_id=mir_id,
            worker_id="worker-x",
        )
        assert claim.claimed is False


@pytest.mark.asyncio
async def test_duplicate_response_no_second_outbox(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, actor = await _enter_waiting(db_session_factory, monkeypatch)
    responses = {"city": "Seoul", "units": "c"}

    async with db_session_factory() as session:
        first = await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
            responses=responses,
        )
        await session.commit()
        assert first.resume_enqueued is True

        second = await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
            responses=responses,
        )
        await session.commit()
        assert second.resume_enqueued is False
        assert second.status == McpInputRequestStatus.ANSWERED.value

        with pytest.raises(AppError) as exc:
            await MrtrResponseService(session).submit_response(
                execution_id=execution_id,
                input_request_id=mir_id,
                actor_user_id=actor,
                responses={"city": "Other", "units": "c"},
            )
        assert exc.value.code == "RESOURCE_CONFLICT"

        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_MRTR_RESUME",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


@pytest.mark.asyncio
async def test_duplicate_resume_claim_stale(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, actor = await _enter_waiting(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
            responses={"city": "Seoul", "units": "c"},
        )
        await session.commit()

        first = await MrtrResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            input_request_id=mir_id,
            worker_id="worker-a",
        )
        await session.commit()
        assert first.claimed is True

        second = await MrtrResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            input_request_id=mir_id,
            worker_id="worker-b",
        )
        assert second.claimed is False
        assert second.reason == "STALE_DELIVERY"


@pytest.mark.asyncio
async def test_forged_response_keys_rejected(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, actor = await _enter_waiting(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await MrtrResponseService(session).submit_response(
                execution_id=execution_id,
                input_request_id=mir_id,
                actor_user_id=actor,
                responses={"city": "Seoul", "units": "c", "extra": True},
            )
        assert exc.value.code == "VALIDATION_ERROR"

        with pytest.raises(AppError) as exc2:
            await MrtrResponseService(session).submit_response(
                execution_id=execution_id,
                input_request_id=mir_id,
                actor_user_id=actor,
                responses={"city": "Seoul", "units": "c", "requestState": "x"},
            )
        assert exc2.value.code == "VALIDATION_ERROR"

        mir = await MCPInputRequestRepository(session).get(mir_id)
        assert mir is not None
        assert mir.status == McpInputRequestStatus.OPEN.value


@pytest.mark.asyncio
async def test_resume_wrong_lineage_fails_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, actor = await _enter_waiting(db_session_factory, monkeypatch)
    async with db_session_factory() as session:
        await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
            responses={"city": "Seoul", "units": "c"},
        )
        await session.commit()

        # Corrupt ToolCall evidence before claim.
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempt = (await ExecutionRepository(session).list_attempts(step.id))[0]
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempt.id)
        tool_calls[0].normalized_status = ToolCallNormalizedStatus.FAILED.value
        await session.commit()

        claim = await MrtrResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            input_request_id=mir_id,
            worker_id="worker-a",
        )
        await session.commit()
        assert claim.claimed is False
        assert claim.reason == "MRTR_RESUME_PRECONDITION_FAILED"
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value


@pytest.mark.asyncio
async def test_outbox_relay_publishes_mrtr_resume(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, mir_id, actor = await _enter_waiting(db_session_factory, monkeypatch)

    class Pub:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def publish_execution(self, **_k: Any) -> None:
            raise AssertionError("dispatch unexpected")

        def publish_approval_resume(self, **_k: Any) -> None:
            raise AssertionError("approval unexpected")

        def publish_mrtr_resume(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    async with db_session_factory() as session:
        await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=actor,
            responses={"city": "Seoul", "units": "c"},
        )
        await session.commit()
        pub = Pub()
        result = await OutboxRelayService(session).publish_batch(
            publisher=pub, limit=10
        )
        await session.commit()
        assert result.published >= 1
        assert len(pub.calls) == 1
        assert pub.calls[0]["execution_id"] == execution_id
        assert pub.calls[0]["input_request_id"] == mir_id
