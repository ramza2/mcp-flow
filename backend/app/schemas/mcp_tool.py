"""Pydantic API schemas for MCP Tool endpoints (docs/06 §9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import MCPToolStatus, ToolVersionValidationStatus


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
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
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
    display_name: str | None = Field(default=None, max_length=255)
    description_override: str | None = None
    tags: list[Any] | None = None
    lock_version: int | None = Field(default=None, ge=1)
