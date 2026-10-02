"""Schemas for Execution cancellation API (docs/06 §14.4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import ExecutionStatus


class ExecutionCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ExecutionCancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    status: ExecutionStatus
    cancel_requested_at: datetime | None = None
    finished_at: datetime | None = None
