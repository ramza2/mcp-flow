"""Pydantic API schemas for Agent registry (docs/06 §10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    AgentStatus,
    AgentToolGrantEffect,
    AgentVersionStatus,
    AgentVersionValidationStatus,
    AgentVisibility,
)

CANONICAL_REQUEST_SCHEMA_VERSION = "1.0"
CANONICAL_PLAN_SCHEMA_VERSION = "1.0"


class SelectionSettings(BaseModel):
    """Known AgentVersion selection_settings keys only (docs/06 §10)."""

    model_config = ConfigDict(extra="forbid")

    auto_select_threshold: float = Field(ge=0.0, le=1.0)
    confirmation_threshold: float = Field(ge=0.0, le=1.0)
    max_candidates: int = Field(ge=1)

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> SelectionSettings:
        if self.auto_select_threshold < self.confirmation_threshold:
            raise ValueError(
                "auto_select_threshold must be >= confirmation_threshold."
            )
        return self


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    visibility: AgentVisibility = AgentVisibility.PRIVATE


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    visibility: AgentVisibility | None = None
    status: AgentStatus | None = None
    lock_version: int | None = Field(default=None, ge=1)


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    status: AgentStatus
    current_version_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    visibility: AgentVisibility
    created_at: datetime
    created_by: uuid.UUID | None = None
    updated_at: datetime
    updated_by: uuid.UUID | None = None
    lock_version: int


class AgentListResponse(BaseModel):
    items: list[AgentResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class AgentVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_instruction: str | None = Field(default=None, min_length=1)
    llm_profile_id: uuid.UUID | None = None
    request_schema_version: str | None = Field(default=None, min_length=1)
    plan_schema_version: str | None = Field(default=None, min_length=1)
    selection_settings: SelectionSettings | None = None
    planning_settings: dict[str, Any] | None = None
    response_settings: dict[str, Any] | None = None
    change_summary: str | None = None
    source_version_id: uuid.UUID | None = None

    @field_validator("planning_settings", "response_settings")
    @classmethod
    def _settings_must_be_object(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("settings must be a JSON object.")
        return value

    @model_validator(mode="after")
    def _require_fields_without_source(self) -> AgentVersionCreate:
        if self.source_version_id is not None:
            return self
        missing: list[str] = []
        if not self.system_instruction:
            missing.append("system_instruction")
        if self.llm_profile_id is None:
            missing.append("llm_profile_id")
        if self.selection_settings is None:
            missing.append("selection_settings")
        if missing:
            raise ValueError(f"Required without source_version_id: {', '.join(missing)}")
        if self.request_schema_version is None:
            self.request_schema_version = CANONICAL_REQUEST_SCHEMA_VERSION
        if self.plan_schema_version is None:
            self.plan_schema_version = CANONICAL_PLAN_SCHEMA_VERSION
        if self.planning_settings is None:
            self.planning_settings = {}
        if self.response_settings is None:
            self.response_settings = {}
        return self


class AgentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_no: int
    status: AgentVersionStatus
    system_instruction: str
    llm_profile_id: uuid.UUID
    request_schema_version: str
    plan_schema_version: str
    selection_settings: dict[str, Any]
    planning_settings: dict[str, Any]
    response_settings: dict[str, Any]
    validation_status: AgentVersionValidationStatus
    validation_report: dict[str, Any] | None = None
    content_hash: str
    change_summary: str | None = None
    published_at: datetime | None = None
    published_by: uuid.UUID | None = None
    deprecated_at: datetime | None = None
    deprecated_by: uuid.UUID | None = None
    created_at: datetime
    created_by: uuid.UUID | None = None


class AgentVersionListResponse(BaseModel):
    items: list[AgentVersionResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class AgentVersionValidationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_no: int
    status: AgentVersionStatus
    validation_status: AgentVersionValidationStatus
    validation_report: dict[str, Any] | None = None


class AgentToolGrantItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mcp_tool_id: uuid.UUID
    effect: AgentToolGrantEffect
    parameter_constraints: dict[str, Any] | None = None
    requires_confirmation: bool = False

    @field_validator("parameter_constraints")
    @classmethod
    def _constraints_object_or_null(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("parameter_constraints must be a JSON object or null.")
        return value


class AgentToolGrantPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AgentToolGrantItem]


class AgentToolGrantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_version_id: uuid.UUID
    mcp_tool_id: uuid.UUID
    effect: AgentToolGrantEffect
    parameter_constraints: dict[str, Any] | None = None
    requires_confirmation: bool
    created_at: datetime
    created_by: uuid.UUID | None = None


class AgentToolGrantListResponse(BaseModel):
    items: list[AgentToolGrantResponse]
