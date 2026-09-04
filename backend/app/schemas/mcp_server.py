"""Pydantic API schemas for MCP Server endpoints (docs/06 §8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    MCPAuthType,
    MCPCheckStatus,
    MCPCheckType,
    MCPDiscoveryMode,
    MCPProtocolEra,
    MCPServerStatus,
    MCPTransportType,
)


class MCPServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    transport_type: MCPTransportType
    endpoint_url: str | None = None
    stdio_manifest_id: str | None = None
    auth_type: MCPAuthType = MCPAuthType.NONE
    auth_secret_id: uuid.UUID | None = None
    connect_timeout_ms: int = Field(default=10000, ge=1)
    call_timeout_ms: int = Field(default=60000, ge=1)
    max_concurrency: int = Field(default=5, ge=1)


class MCPServerUpdate(BaseModel):
    """PATCH body — status transitions use activate/deactivate actions only."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    transport_type: MCPTransportType | None = None
    endpoint_url: str | None = None
    stdio_manifest_id: str | None = None
    auth_type: MCPAuthType | None = None
    auth_secret_id: uuid.UUID | None = None
    connect_timeout_ms: int | None = Field(default=None, ge=1)
    call_timeout_ms: int | None = Field(default=None, ge=1)
    max_concurrency: int | None = Field(default=None, ge=1)
    lock_version: int | None = Field(default=None, ge=1)


class MCPServerResponse(BaseModel):
    """Public MCP Server fields — no secret material."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    transport_type: MCPTransportType
    endpoint_url: str | None = None
    stdio_manifest_id: str | None = None
    auth_type: MCPAuthType
    auth_secret_id: uuid.UUID | None = None
    status: MCPServerStatus
    protocol_era: MCPProtocolEra
    discovery_mode: MCPDiscoveryMode | None = None
    negotiated_protocol_version: str | None = None
    capabilities: dict[str, Any] | None = None
    connect_timeout_ms: int
    call_timeout_ms: int
    max_concurrency: int
    last_healthy_at: datetime | None = None
    last_error_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    lock_version: int


class MCPServerListResponse(BaseModel):
    items: list[MCPServerResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class ConnectionTestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_server_id: uuid.UUID
    check_type: MCPCheckType
    status: MCPCheckStatus
    latency_ms: int | None = None
    protocol_version: str | None = None
    discovery_mode: MCPDiscoveryMode | None = None
    error_layer: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    checked_at: datetime


class ConnectionTestListResponse(BaseModel):
    items: list[ConnectionTestResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class DiscoveryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "FULL"
    apply_changes: bool = False


class DiscoveryDiffSummary(BaseModel):
    added: int = 0
    changed: int = 0
    missing: int = 0
    unchanged: int = 0


class DiscoveryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_server_id: uuid.UUID
    protocol_era: MCPProtocolEra
    discovery_mode: MCPDiscoveryMode | None = None
    selected_version: str | None = None
    success: bool
    error_code: str | None = None
    error_message: str | None = None
    apply_changes: bool = False
    diff: DiscoveryDiffSummary = Field(default_factory=DiscoveryDiffSummary)
    started_at: datetime
    finished_at: datetime | None = None
    capabilities: dict[str, Any] | None = None


class DiscoveryListResponse(BaseModel):
    items: list[DiscoveryResponse]
    page: int
    page_size: int
    total: int
    has_next: bool
