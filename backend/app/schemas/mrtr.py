"""MRTR Input API schemas (docs/06 §15) — safe projection only."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MrtrInputRequestItem(BaseModel):
    id: uuid.UUID
    status: str
    source: str = "MCP_MRTR"
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    round_no: int
    input_requests: dict[str, Any]
    expires_at: datetime
    requested_at: datetime
    answered_at: datetime | None = None


class MrtrInputRequestListResponse(BaseModel):
    items: list[MrtrInputRequestItem]


class MrtrResponseCreateRequest(BaseModel):
    responses: dict[str, Any] = Field(
        ...,
        description="Map of input request keys to user-provided values (no requestState).",
    )


class MrtrResponseCreateResponse(BaseModel):
    input_request_id: uuid.UUID
    execution_id: uuid.UUID
    status: str
    resume_enqueued: bool
    execution_status: str
    step_status: str


class MrtrRejectResponse(BaseModel):
    input_request_id: uuid.UUID
    execution_id: uuid.UUID
    status: str
    execution_status: str
    step_status: str
