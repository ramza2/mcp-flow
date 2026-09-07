"""Pydantic API schemas for MCP Tool endpoints (docs/06 §9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import (
    MCPToolStatus,
    RiskClass,
    ToolVerificationStatus,
    ToolVersionValidationStatus,
)

_MAX_TAGS = 32
_MAX_TAG_LEN = 64


def normalize_tags(tags: list[Any] | None) -> list[str] | None:
    if tags is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            raise ValueError("tags must be a list of strings")
        value = raw.strip()
        if not value:
            continue
        if len(value) > _MAX_TAG_LEN:
            raise ValueError(f"each tag must be <= {_MAX_TAG_LEN} characters")
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
        if len(cleaned) > _MAX_TAGS:
            raise ValueError(f"tags must contain at most {_MAX_TAGS} items")
    return cleaned


class MCPToolResponse(BaseModel):
    """Public MCP Tool fields — no secret material."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_server_id: uuid.UUID
    remote_name: str
    display_name: str | None = None
    description_override: str | None = None
    tags: list[Any] | None = None
    status: MCPToolStatus
    current_version_id: uuid.UUID | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime
    lock_version: int


class MCPToolListResponse(BaseModel):
    items: list[MCPToolResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class MCPToolVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_tool_id: uuid.UUID
    version_no: int
    remote_description: str | None = None
    input_schema: Any | None = None
    output_schema: Any | None = None
    annotations: dict[str, Any] | None = None
    schema_dialect: str | None = None
    content_hash: str
    validation_status: ToolVersionValidationStatus
    validation_errors: list[Any] | None = None
    discovered_at: datetime
    created_at: datetime


class MCPToolVersionListResponse(BaseModel):
    items: list[MCPToolVersionResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class MCPToolUpdate(BaseModel):
    """Operator-editable metadata only (docs/05). Status changes use action endpoints."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=255)
    description_override: str | None = None
    tags: list[str] | None = None
    lock_version: int | None = Field(default=None, ge=1)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str] | None) -> list[str] | None:
        return normalize_tags(value)


class MCPToolPolicyPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_class: RiskClass
    requires_confirmation: bool = False
    requires_approval: bool = False
    approval_policy_id: uuid.UUID | None = None
    timeout_ms: int = Field(..., gt=0)
    max_attempts: int = Field(..., ge=1)
    backoff_policy: dict[str, Any] | None = None
    max_result_bytes: int = Field(..., gt=0)
    allow_auto_select: bool = True
    data_classification: str | None = Field(default=None, max_length=64)
    policy_metadata: dict[str, Any] | None = None
    lock_version: int | None = Field(default=None, ge=1)


class MCPToolPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_tool_id: uuid.UUID
    risk_class: RiskClass
    requires_confirmation: bool
    requires_approval: bool
    approval_policy_id: uuid.UUID | None = None
    timeout_ms: int
    max_attempts: int
    backoff_policy: dict[str, Any] | None = None
    max_result_bytes: int
    allow_auto_select: bool
    data_classification: str | None = None
    policy_metadata: dict[str, Any] | None = None
    updated_at: datetime
    updated_by: uuid.UUID | None = None
    lock_version: int


class ToolVerificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolVerificationStatus
    criteria_version: str = Field(..., min_length=1, max_length=128)
    test_execution_id: uuid.UUID | None = None
    result_summary: dict[str, Any] | None = None
    evidence_blob_id: uuid.UUID | None = None
    expires_at: datetime | None = None


class ToolVerificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_tool_version_id: uuid.UUID
    status: ToolVerificationStatus
    verified_by: uuid.UUID | None = None
    verified_at: datetime
    test_execution_id: uuid.UUID | None = None
    criteria_version: str
    result_summary: dict[str, Any] | None = None
    evidence_blob_id: uuid.UUID | None = None
    expires_at: datetime | None = None


class ToolVerificationListResponse(BaseModel):
    items: list[ToolVerificationResponse]
    page: int
    page_size: int
    total: int
    has_next: bool
