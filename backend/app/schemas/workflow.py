"""Pydantic API schemas for Workflow registry (docs/06 §12)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    WorkflowStatus,
    WorkflowVersionStatus,
    WorkflowVersionValidationStatus,
    WorkflowVisibility,
)

CANONICAL_PLAN_SCHEMA_VERSION = "1.0"


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    visibility: WorkflowVisibility = WorkflowVisibility.PRIVATE


class WorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    visibility: WorkflowVisibility | None = None
    status: WorkflowStatus | None = None
    lock_version: int | None = Field(default=None, ge=1)


class WorkflowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    status: WorkflowStatus
    current_version_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    visibility: WorkflowVisibility
    created_at: datetime
    created_by: uuid.UUID | None = None
    updated_at: datetime
    updated_by: uuid.UUID | None = None
    lock_version: int


class WorkflowListResponse(BaseModel):
    items: list[WorkflowResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class WorkflowVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_definition: dict[str, Any] | None = None
    plan_schema_version: str | None = Field(default=None, min_length=1)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    policy_defaults: dict[str, Any] | None = None
    change_summary: str | None = None
    source_version_id: uuid.UUID | None = None

    @field_validator(
        "plan_definition", "input_schema", "output_schema", "policy_defaults"
    )
    @classmethod
    def _must_be_object(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("must be a JSON object.")
        return value

    @model_validator(mode="after")
    def _require_fields_without_source(self) -> WorkflowVersionCreate:
        if self.source_version_id is not None:
            return self
        if self.plan_definition is None:
            raise ValueError("plan_definition is required without source_version_id")
        if self.plan_schema_version is None:
            self.plan_schema_version = CANONICAL_PLAN_SCHEMA_VERSION
        if self.input_schema is None:
            self.input_schema = {}
        if self.output_schema is None:
            self.output_schema = {}
        if self.policy_defaults is None:
            self.policy_defaults = {}
        return self


class WorkflowVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    version_no: int
    status: WorkflowVersionStatus
    plan_schema_version: str
    plan_definition: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    policy_defaults: dict[str, Any]
    validation_status: WorkflowVersionValidationStatus
    validation_report: dict[str, Any] | None = None
    content_hash: str
    change_summary: str | None = None
    published_at: datetime | None = None
    published_by: uuid.UUID | None = None
    deprecated_at: datetime | None = None
    deprecated_by: uuid.UUID | None = None
    created_at: datetime
    created_by: uuid.UUID | None = None


class WorkflowVersionListResponse(BaseModel):
    items: list[WorkflowVersionResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class WorkflowPlanPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_definition: dict[str, Any]
    change_summary: str | None = None

    @field_validator("plan_definition")
    @classmethod
    def _plan_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("plan_definition must be a JSON object.")
        return value


class WorkflowVersionValidationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    version_no: int
    status: WorkflowVersionStatus
    validation_status: WorkflowVersionValidationStatus
    validation_report: dict[str, Any] | None = None
    content_hash: str


class WorkflowExecutionCreateRequest(BaseModel):
    """POST .../versions/{version_id}/executions request body."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any]


class WorkflowExecutionCreateResult(BaseModel):
    """POST .../versions/{version_id}/executions success body."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    status: str
    source_type: str
    trigger_type: str
    workflow_version_id: uuid.UUID
    plan_hash: str = Field(min_length=64, max_length=64)
    requested_at: datetime
    step_count: int = Field(ge=1)
