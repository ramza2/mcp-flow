"""Approval API schemas (docs/06 §13) — decisions + query/inbox."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ApprovalDecisionCreateRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = None


class ApprovalDecisionResponse(BaseModel):
    decision_id: uuid.UUID
    approval_request_id: uuid.UUID
    approval_status: str
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    execution_status: str
    step_status: str
    decision: str
    decided_by: uuid.UUID
    decided_at: datetime
    resume_enqueued: bool = Field(
        description="True when APPROVED created durable EXECUTION_APPROVAL_RESUME Outbox."
    )


class ApprovalListItemResponse(BaseModel):
    id: uuid.UUID
    status: str
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    requested_by: uuid.UUID
    decision_mode: str
    required_approvals: int
    requested_at: datetime
    expires_at: datetime
    resolved_at: datetime | None
    approve_count: int
    reject_count: int
    can_decide: bool


class ApprovalListResponse(BaseModel):
    items: list[ApprovalListItemResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class ApprovalDecisionHistoryItem(BaseModel):
    decision_id: uuid.UUID
    decided_by: uuid.UUID
    decision: str
    comment: str | None
    decided_at: datetime


class ApprovalDetailResponse(ApprovalListItemResponse):
    safe_context: dict[str, Any]
    decisions: list[ApprovalDecisionHistoryItem]
