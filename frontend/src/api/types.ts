/**
 * Frontend API DTOs — aligned with backend/app/schemas/mcp_*.py (docs/06).
 * Do not invent fields from mock.ts.
 */

import type {
  MCPAuthType,
  MCPCheckStatus,
  MCPCheckType,
  MCPDiscoveryMode,
  MCPProtocolEra,
  MCPServerStatus,
  MCPToolStatus,
  MCPTransportType,
  ToolVersionValidationStatus,
} from '../domain/types';

/** Arbitrary JSON value — ToolVersion schemas may be malformed remote payloads. */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface PageDto<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: unknown[];
    request_id?: string;
    retryable?: boolean;
  };
}

export interface MCPServerDto {
  id: string;
  code: string;
  name: string;
  description: string | null;
  transport_type: MCPTransportType;
  endpoint_url: string | null;
  stdio_manifest_id: string | null;
  auth_type: MCPAuthType;
  auth_secret_id: string | null;
  status: MCPServerStatus;
  protocol_era: MCPProtocolEra;
  discovery_mode: MCPDiscoveryMode | null;
  negotiated_protocol_version: string | null;
  capabilities: Record<string, JsonValue> | null;
  connect_timeout_ms: number;
  call_timeout_ms: number;
  max_concurrency: number;
  last_healthy_at: string | null;
  last_error_at: string | null;
  created_at: string;
  updated_at: string;
  lock_version: number;
}

export type MCPServerListDto = PageDto<MCPServerDto>;

export interface MCPServerCreateRequest {
  name: string;
  description?: string | null;
  transport_type: MCPTransportType;
  endpoint_url?: string | null;
  stdio_manifest_id?: string | null;
  auth_type?: MCPAuthType;
  auth_secret_id?: string | null;
  connect_timeout_ms?: number;
  call_timeout_ms?: number;
  max_concurrency?: number;
}

export interface ConnectionTestDto {
  id: string;
  mcp_server_id: string;
  check_type: MCPCheckType;
  status: MCPCheckStatus;
  latency_ms: number | null;
  protocol_version: string | null;
  discovery_mode: MCPDiscoveryMode | null;
  error_layer: string | null;
  error_code: string | null;
  error_message: string | null;
  checked_at: string;
}

export interface DiscoveryDiffSummaryDto {
  added: number;
  changed: number;
  missing: number;
  unchanged: number;
}

export interface DiscoveryCreateRequest {
  mode?: string;
  apply_changes?: boolean;
}

export interface DiscoveryDto {
  id: string;
  mcp_server_id: string;
  protocol_era: MCPProtocolEra;
  discovery_mode: MCPDiscoveryMode | null;
  selected_version: string | null;
  success: boolean;
  error_code: string | null;
  error_message: string | null;
  apply_changes: boolean;
  diff: DiscoveryDiffSummaryDto;
  started_at: string;
  finished_at: string | null;
  capabilities: Record<string, JsonValue> | null;
}

export type DiscoveryListDto = PageDto<DiscoveryDto>;

export interface MCPToolDto {
  id: string;
  mcp_server_id: string;
  remote_name: string;
  display_name: string | null;
  description_override: string | null;
  tags: JsonValue[] | null;
  status: MCPToolStatus;
  current_version_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  created_at: string;
  updated_at: string;
  lock_version: number;
}

export type MCPToolListDto = PageDto<MCPToolDto>;

export interface MCPToolVersionDto {
  id: string;
  mcp_tool_id: string;
  version_no: number;
  remote_description: string | null;
  input_schema: JsonValue | null;
  output_schema: JsonValue | null;
  annotations: Record<string, JsonValue> | null;
  schema_dialect: string | null;
  content_hash: string;
  validation_status: ToolVersionValidationStatus;
  validation_errors: JsonValue[] | null;
  discovered_at: string;
  created_at: string;
}

export type MCPToolVersionListDto = PageDto<MCPToolVersionDto>;

export interface ListParams {
  page?: number;
  page_size?: number;
  sort?: string;
  q?: string;
  status?: string;
  transport_type?: string;
  mcp_server_id?: string;
  signal?: AbortSignal;
}
