"""Pydantic API schemas for Schedule registry (docs/06 §17)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleStatus,
    ScheduleTargetType,
    ScheduleType,
    OccurrenceStatus,
)
from app.models.schedule import Schedule, ScheduleOccurrence

_NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {
        "name",
        "target_type",
        "target_id",
        "schedule_type",
        "schedule_expression",
        "timezone",
        "inputs",
        "overlap_policy",
        "misfire_policy",
        "max_catch_up",
    }
)


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    target_type: ScheduleTargetType
    target_id: uuid.UUID
    schedule_type: ScheduleType
    schedule_expression: str = Field(min_length=1)
    timezone: str = Field(min_length=1, max_length=128)
    inputs: dict[str, Any] = Field(default_factory=dict)
    overlap_policy: ScheduleOverlapPolicy | None = None
    misfire_policy: ScheduleMisfirePolicy | None = None
    max_catch_up: int | None = Field(default=None, ge=1, le=100)
    start_at: datetime | None = None
    end_at: datetime | None = None

    @field_validator("inputs")
    @classmethod
    def _inputs_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("inputs must be a JSON object.")
        return value

    @field_validator("start_at", "end_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware RFC3339 values.")
        return value


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    target_type: ScheduleTargetType | None = None
    target_id: uuid.UUID | None = None
    schedule_type: ScheduleType | None = None
    schedule_expression: str | None = Field(default=None, min_length=1)
    timezone: str | None = Field(default=None, min_length=1, max_length=128)
    inputs: dict[str, Any] | None = None
    overlap_policy: ScheduleOverlapPolicy | None = None
    misfire_policy: ScheduleMisfirePolicy | None = None
    max_catch_up: int | None = Field(default=None, ge=1, le=100)
    start_at: datetime | None = None
    end_at: datetime | None = None

    @field_validator("inputs")
    @classmethod
    def _inputs_object(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and not isinstance(value, dict):
            raise ValueError("inputs must be a JSON object.")
        return value

    @field_validator("start_at", "end_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware RFC3339 values.")
        return value

    @model_validator(mode="after")
    def _reject_explicit_null_non_nullable(self) -> ScheduleUpdate:
        # Omitted fields are fine; explicit JSON null on non-nullable fields is not.
        # description / start_at / end_at remain clearable via null.
        for field_name in _NON_NULLABLE_UPDATE_FIELDS:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    owner_id: uuid.UUID
    target_type: ScheduleTargetType
    target_id: uuid.UUID
    schedule_type: ScheduleType
    schedule_expression: str
    timezone: str
    inputs: dict[str, Any]
    misfire_policy: ScheduleMisfirePolicy
    overlap_policy: ScheduleOverlapPolicy
    max_catch_up: int
    status: ScheduleStatus
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    lock_version: int


class ScheduleListResponse(BaseModel):
    items: list[ScheduleResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class ScheduleOccurrenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schedule_id: uuid.UUID
    scheduled_for: datetime
    status: OccurrenceStatus
    decision_reason: str | None = None
    created_at: datetime
    enqueued_at: datetime | None = None
    finished_at: datetime | None = None


class ScheduleOccurrenceListResponse(BaseModel):
    items: list[ScheduleOccurrenceResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


def schedule_to_response(schedule: Schedule) -> ScheduleResponse:
    target_id = schedule.agent_version_id or schedule.workflow_version_id
    if target_id is None:
        raise ValueError("Schedule target_id is missing.")
    return ScheduleResponse(
        id=schedule.id,
        name=schedule.name,
        description=schedule.description,
        owner_id=schedule.owner_id,
        target_type=ScheduleTargetType(schedule.target_type),
        target_id=target_id,
        schedule_type=ScheduleType(schedule.schedule_type),
        schedule_expression=schedule.schedule_expression,
        timezone=schedule.timezone,
        inputs=dict(schedule.input_template or {}),
        misfire_policy=ScheduleMisfirePolicy(schedule.misfire_policy),
        overlap_policy=ScheduleOverlapPolicy(schedule.overlap_policy),
        max_catch_up=schedule.max_catch_up,
        status=ScheduleStatus(schedule.status),
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        start_at=schedule.start_at,
        end_at=schedule.end_at,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
        lock_version=schedule.lock_version,
    )


def occurrence_to_response(row: ScheduleOccurrence) -> ScheduleOccurrenceResponse:
    return ScheduleOccurrenceResponse.model_validate(row)
