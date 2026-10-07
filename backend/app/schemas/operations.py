"""Operations Dashboard / stats / system-health schemas (docs/06 §18).

Aggregate-only projections — no resource IDs, names, endpoints, or secrets.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.execution_query import ExecutionListItem


class ExecutionStatusCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = 0
    created: int = 0
    queued: int = 0
    running: int = 0
    waiting_input: int = 0
    waiting_approval: int = 0
    cancel_requested: int = 0
    succeeded: int = 0
    partially_succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0


class ApprovalOpsSummary(BaseModel):
    pending: int = 0
    overdue: int = 0


class ScheduleOpsSummary(BaseModel):
    active: int = 0
    paused: int = 0
    completed: int = 0
    error: int = 0
    overdue: int = 0


class MCPServerOpsSummary(BaseModel):
    total: int = 0
    active: int = 0
    inactive: int = 0
    error: int = 0
    draft: int = 0


class MCPToolOpsSummary(BaseModel):
    total: int = 0
    discovered: int = 0
    active: int = 0
    inactive: int = 0
    missing: int = 0
    blocked: int = 0
    problematic: int = 0


class DashboardSummaryResponse(BaseModel):
    """GET /ops/dashboard/summary — rolling window aggregates + recent list."""

    model_config = ConfigDict(extra="forbid")

    window_from: datetime
    window_to: datetime
    generated_at: datetime

    executions: ExecutionStatusCounts
    terminal_total: int
    success_rate: float | None = None
    avg_duration_ms: float | None = None
    p95_duration_ms: float | None = None

    approvals: ApprovalOpsSummary
    schedules: ScheduleOpsSummary
    mcp_servers: MCPServerOpsSummary
    mcp_tools: MCPToolOpsSummary

    # Same safe list projection as GET /executions (no pagination wrapper).
    recent_executions: list[ExecutionListItem] = Field(default_factory=list)


class ErrorCodeCount(BaseModel):
    error_code: str
    count: int


class DurationStats(BaseModel):
    avg_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None


class ExecutionStatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_from: datetime
    window_to: datetime
    by_status: dict[str, int]
    by_source_type: dict[str, int]
    by_trigger_type: dict[str, int]
    by_error_category: dict[str, int]
    top_error_codes: list[ErrorCodeCount]
    duration: DurationStats


class DatabaseHealth(BaseModel):
    status: str


class ExecutionQueueHealth(BaseModel):
    created_count: int = 0
    queued_count: int = 0
    running_count: int = 0
    oldest_created_age_seconds: float | None = None
    oldest_queued_age_seconds: float | None = None


class SchedulerHealth(BaseModel):
    overdue_schedule_count: int = 0
    error_schedule_count: int = 0


class OutboxHealth(BaseModel):
    pending_count: int = 0
    oldest_pending_age_seconds: float | None = None


class SystemHealthResponse(BaseModel):
    """Durable/observable signals only — no invented Worker/Redis heartbeats."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    database: DatabaseHealth
    execution_queue: ExecutionQueueHealth
    scheduler: SchedulerHealth
    outbox: OutboxHealth
