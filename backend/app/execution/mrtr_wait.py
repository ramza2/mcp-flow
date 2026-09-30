"""Durable MRTR WAITING_INPUT evidence validation (docs/05 §13.7 / PR #40).

Validates restart-safe wait lineage before Runner no-op reuse.
Does not repair inconsistent state — fail closed as RESOURCE_CONFLICT.
"""

from __future__ import annotations

from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    MCPProtocolEra,
    McpInputRequestStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.lineage import assert_resume_attempt_lineage
from app.models.execution import Execution, ExecutionStep
from app.models.mcp_input_request import MCPInputRequest
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_input_request import MCPInputRequestRepository


def _conflict(message: str) -> AppError:
    return AppError(
        code="RESOURCE_CONFLICT",
        message=message,
        status_code=409,
    )


async def assert_durable_waiting_input(
    *,
    executions: ExecutionRepository,
    inputs: MCPInputRequestRepository,
    execution: Execution,
    step: ExecutionStep,
) -> MCPInputRequest:
    """Validate durable WAITING_INPUT evidence for the current OPEN MRTR round.

    Returns the single OPEN ``MCPInputRequest`` when consistent.
    Raises ``RESOURCE_CONFLICT`` on any invariant failure (no repair).

    Round N wait requires exactly N SUCCEEDED ToolCall rounds on the STARTED
    Attempt (``request.round_no == len(succeeded)``).
    """
    if execution.status != ExecutionStatus.WAITING_INPUT.value:
        raise _conflict("Execution is not WAITING_INPUT.")
    if step.status != StepStatus.WAITING_INPUT.value:
        raise _conflict("Step is not WAITING_INPUT.")
    if step.execution_id != execution.id:
        raise _conflict("Step lineage does not match Execution.")

    if (
        execution.worker_id is not None
        or execution.lease_token is not None
        or execution.lease_expires_at is not None
        or execution.heartbeat_at is not None
    ):
        raise _conflict("WAITING_INPUT Execution must not retain worker lease fields.")

    open_rows = await inputs.list_open_for_step(
        execution_id=execution.id, step_execution_id=step.id
    )
    if len(open_rows) == 0:
        raise _conflict("WAITING_INPUT is missing OPEN MCPInputRequest evidence.")
    if len(open_rows) > 1:
        raise _conflict("WAITING_INPUT has multiple OPEN MCPInputRequest rows.")
    request = open_rows[0]

    if (
        request.execution_id != execution.id
        or request.step_execution_id != step.id
        or request.status != McpInputRequestStatus.OPEN.value
        or request.protocol_era != MCPProtocolEra.CURRENT.value
        or not isinstance(request.round_no, int)
        or isinstance(request.round_no, bool)
        or request.round_no < 1
    ):
        raise _conflict("OPEN MCPInputRequest lineage is inconsistent.")

    attempts = await executions.list_attempts(step.id)
    started = [a for a in attempts if a.status == StepAttemptStatus.STARTED.value]
    if len(started) != 1:
        raise _conflict("WAITING_INPUT requires exactly one STARTED Attempt.")
    attempt = started[0]
    if attempt.id != request.step_attempt_id:
        raise _conflict("OPEN MCPInputRequest does not reference the STARTED Attempt.")

    if (
        attempt.worker_id is not None
        or attempt.lease_expires_at is not None
        or attempt.finished_at is not None
    ):
        raise _conflict("WAITING_INPUT Attempt must clear worker/lease and stay unfinished.")

    # Immutable Attempt/Step plan+binding lineage (worker ownership not required).
    assert_resume_attempt_lineage(
        execution=execution,
        step=step,
        attempt=attempt,
        worker_id=None,
    )
    if attempt.attempt_no != step.attempt_count:
        raise _conflict("STARTED Attempt attempt_no does not match Step.attempt_count.")

    tool_calls = await executions.list_tool_calls(attempt.id)
    if any(
        tc.normalized_status == ToolCallNormalizedStatus.STARTED.value
        for tc in tool_calls
    ):
        raise _conflict("WAITING_INPUT must not retain a STARTED ToolCall.")
    succeeded = [
        tc
        for tc in tool_calls
        if tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
    ]
    if not succeeded:
        raise _conflict("WAITING_INPUT requires SUCCEEDED ToolCall round evidence.")
    # Every ToolCall on the STARTED Attempt must be a completed SUCCEEDED round,
    # and the OPEN request round_no must equal that completed count.
    if len(tool_calls) != len(succeeded) or len(succeeded) != request.round_no:
        raise _conflict("WAITING_INPUT ToolCall evidence does not match round_no.")

    return request
