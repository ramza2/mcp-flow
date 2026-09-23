"""Approval decision API schemas (docs/06 §13)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

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
