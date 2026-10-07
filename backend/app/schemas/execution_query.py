"""Execution history / operations read schemas (docs/06 ops + FNC-OPS-002).

Safe scalar projections only — never ORM dumps of snapshots / leases / secrets.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    ExecutionTriggerType,
)
from app.ops.error_category import ErrorCategory

ExecutionSort = Literal[
    "requested_at",
    "-requested_at",
    "started_at",
    "-started_at",
    "finished_at",
    "-finished_at",
    "status",
    "-status",
]


class ExecutionSourceProjection(BaseModel):
    type: ExecutionSourceType
    version_id: uuid.UUID | None = None
    logical_id: uuid.UUID | None = None
    name: str | None = None


class ExecutionListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    source_type: ExecutionSourceType
    trigger_type: ExecutionTriggerType
    requester_id: uuid.UUID

    agent_request_id: uuid.UUID | None = None
    agent_version_id: uuid.UUID | None = None
    workflow_version_id: uuid.UUID | None = None
    schedule_occurrence_id: uuid.UUID | None = None
    parent_execution_id: uuid.UUID | None = None

    status: ExecutionStatus
    error_code: str | None = None
    error_category: ErrorCategory | None = None
    trace_id: str | None = None

    requested_at: datetime
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None

    step_count: int = 0
    completed_step_count: int = 0
    failed_step_count: int = 0

    duration_ms: int | None = None
    source: ExecutionSourceProjection | None = None


class ExecutionListResponse(BaseModel):
    items: list[ExecutionListItem]
    page: int
    page_size: int
    total: int


class PlanLimitsSummary(BaseModel):
    max_steps: int | None = None
    max_duration_seconds: int | None = None
    max_parallelism: int | None = None
    max_loop_iterations: int | None = None


class ExecutionDetail(ExecutionListItem):
    plan_schema_version: str
    plan_hash: str
    plan_limits: PlanLimitsSummary | None = None
    result_summary: dict[str, Any] | None = None
    retention_until: datetime | None = None


class ToolCallSafeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    mcp_server_id: uuid.UUID
    mcp_tool_version_id: uuid.UUID
    protocol_era: str
    protocol_version: str
    transport_type: str
    normalized_status: str
    request_bytes: int | None = None
    response_bytes: int | None = None
    started_at: datetime
    first_byte_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    time_to_first_byte_ms: int | None = None


class StepAttemptSafeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    attempt_no: int
    status: str
    error_layer: str | None = None
    error_code: str | None = None
    error_category: ErrorCategory | None = None
    is_retryable: bool | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    tool_calls: list[ToolCallSafeItem] = Field(default_factory=list)


class ExecutionStepListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    execution_id: uuid.UUID
    step_key: str
    step_type: str
    parent_step_id: uuid.UUID | None = None
    sequence_hint: int
    mcp_tool_version_id: uuid.UUID | None = None
    iteration_no: int | None = None
    status: str
    attempt_count: int
    condition_result: bool | None = None
    ready_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_category: ErrorCategory | None = None
    duration_ms: int | None = None


class ExecutionStepListResponse(BaseModel):
    items: list[ExecutionStepListItem]


class ExecutionStepDetail(ExecutionStepListItem):
    attempts: list[StepAttemptSafeItem] = Field(default_factory=list)
