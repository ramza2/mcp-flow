/**
 * Frontend API DTOs — aligned with backend/app/schemas/mcp_*.py (docs/06).
 * Do not invent fields from mock.ts.
 */

import type {
  AgentStatus,
  AgentVersionStatus,
  AgentVersionValidationStatus,
  AgentVisibility,
  ExternalMCPReviewDecision,
  ExternalMCPReviewState,
  ExternalMCPSearchStatus,
  ExternalMCPSourceType,
  MCPAuthType,
  MCPCheckStatus,
  MCPCheckType,
  MCPDiscoveryMode,
  MCPProtocolEra,
  MCPServerStatus,
  MCPToolStatus,
  MCPTransportType,
  RiskClass,
  ToolVerificationStatus,
  ToolVersionValidationStatus,
} from '../domain/types';

export type {
  ExternalMCPReviewDecision,
  ExternalMCPReviewState,
  ExternalMCPSearchStatus,
  ExternalMCPSourceType,
};

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

/** Operator-editable metadata only — status uses activate/deactivate. */
export interface MCPToolUpdateRequest {
  display_name?: string | null;
  description_override?: string | null;
  tags?: string[] | null;
}

export interface MCPToolPolicyPutRequest {
  risk_class: RiskClass;
  requires_confirmation?: boolean;
  requires_approval?: boolean;
  approval_policy_id?: string | null;
  timeout_ms: number;
  max_attempts: number;
  backoff_policy?: Record<string, JsonValue> | null;
  max_result_bytes: number;
  allow_auto_select?: boolean;
  data_classification?: string | null;
  policy_metadata?: Record<string, JsonValue> | null;
}

export interface MCPToolPolicyDto {
  id: string;
  mcp_tool_id: string;
  risk_class: RiskClass;
  requires_confirmation: boolean;
  requires_approval: boolean;
  approval_policy_id: string | null;
  timeout_ms: number;
  max_attempts: number;
  backoff_policy: Record<string, JsonValue> | null;
  max_result_bytes: number;
  allow_auto_select: boolean;
  data_classification: string | null;
  policy_metadata: Record<string, JsonValue> | null;
  updated_at: string;
  updated_by: string | null;
  lock_version: number;
}

/** FNC-TOOL-005 Manual Tool Test create (no Verification VERIFIED). */
export interface ManualToolTestCreateRequest {
  mcp_tool_version_id: string;
  arguments: Record<string, JsonValue>;
}

export interface ManualToolTestCreateResult {
  id: string;
  status: string;
  source_type: string;
  trigger_type: string;
  mcp_tool_id: string;
  mcp_tool_version_id: string;
  plan_hash: string;
  requested_at: string;
  step_count: number;
}

/** FNC-TOOL-006: VERIFIED derives result_summary / evidence_blob_id server-side. */
export interface ToolVerificationCreateRequest {
  status: ToolVerificationStatus;
  criteria_version: string;
  test_execution_id?: string | null;
  error_test_execution_id?: string | null;
  /** PENDING / FAILED only — rejected for VERIFIED. */
  result_summary?: Record<string, JsonValue> | null;
  /** Client-supplied evidence_blob_id is rejected for VERIFIED. */
  evidence_blob_id?: string | null;
  expires_at?: string | null;
}

export interface ToolVerificationDto {
  id: string;
  mcp_tool_version_id: string;
  status: ToolVerificationStatus;
  verified_by: string | null;
  verified_at: string;
  test_execution_id: string | null;
  criteria_version: string;
  result_summary: Record<string, JsonValue> | null;
  evidence_blob_id: string | null;
  expires_at: string | null;
}

export type ToolVerificationListDto = PageDto<ToolVerificationDto>;

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

/** Agent registry — backend/app/schemas/agent.py (docs/06 §10). */

export interface AgentDto {
  id: string;
  code: string;
  name: string;
  description: string | null;
  status: AgentStatus;
  current_version_id: string | null;
  owner_id: string | null;
  visibility: AgentVisibility;
  created_at: string;
  created_by: string | null;
  updated_at: string;
  updated_by: string | null;
  lock_version: number;
}

export type AgentListDto = PageDto<AgentDto>;

export interface AgentCreateRequest {
  name: string;
  description?: string | null;
  visibility?: AgentVisibility;
}

export interface AgentUpdateRequest {
  name?: string;
  description?: string | null;
  visibility?: AgentVisibility;
  status?: AgentStatus;
  lock_version?: number;
}

export interface AgentSelectionSettings {
  auto_select_threshold: number;
  confirmation_threshold: number;
  max_candidates: number;
}

export interface AgentVersionDto {
  id: string;
  agent_id: string;
  version_no: number;
  status: AgentVersionStatus;
  system_instruction: string;
  llm_profile_id: string;
  request_schema_version: string;
  plan_schema_version: string;
  selection_settings: AgentSelectionSettings | Record<string, JsonValue>;
  planning_settings: Record<string, JsonValue>;
  response_settings: Record<string, JsonValue>;
  validation_status: AgentVersionValidationStatus;
  validation_report: Record<string, JsonValue> | null;
  content_hash: string;
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
  deprecated_at: string | null;
  deprecated_by: string | null;
  created_at: string;
  created_by: string | null;
}

export type AgentVersionListDto = PageDto<AgentVersionDto>;

export interface AgentVersionCreateRequest {
  system_instruction?: string;
  llm_profile_id?: string;
  request_schema_version?: string;
  plan_schema_version?: string;
  selection_settings?: AgentSelectionSettings;
  planning_settings?: Record<string, JsonValue>;
  response_settings?: Record<string, JsonValue>;
  change_summary?: string | null;
  source_version_id?: string | null;
}

export interface AgentVersionValidationDto {
  id: string;
  agent_id: string;
  version_no: number;
  status: AgentVersionStatus;
  validation_status: AgentVersionValidationStatus;
  validation_report: Record<string, JsonValue> | null;
}

export interface AgentToolGrantDto {
  agent_version_id: string;
  mcp_tool_id: string;
  effect: 'ALLOW' | 'DENY';
  parameter_constraints: Record<string, JsonValue> | null;
  requires_confirmation: boolean;
  created_at: string;
  created_by: string | null;
}

export interface AgentToolGrantListDto {
  items: AgentToolGrantDto[];
}

export interface AgentToolGrantPutRequest {
  items: Array<{
    mcp_tool_id: string;
    effect: 'ALLOW' | 'DENY';
    parameter_constraints?: Record<string, JsonValue> | null;
    requires_confirmation?: boolean;
  }>;
}

export interface LLMProfileDto {
  id: string;
  code: string;
  name: string;
  provider: string;
  model: string;
  base_url: string;
  credential_secret_id: string | null;
  parameters: Record<string, JsonValue> | null;
  request_timeout_ms: number | null;
  status: string | null;
  created_at: string;
  updated_at: string;
  lock_version: number;
}

export type LLMProfileListDto = PageDto<LLMProfileDto>;

/** External MCP Discovery — backend/app/schemas/external_discovery.py (docs/06 §19). */

export interface ExternalMCPSourceDto {
  id: string;
  code: string;
  name: string;
  source_type: ExternalMCPSourceType;
  provider_key: string;
  base_url: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ExternalMCPSourceListDto {
  items: ExternalMCPSourceDto[];
}

export interface ExternalMCPSearchCreateRequest {
  source_id: string;
  q: string;
  limit?: number;
}

export interface ExternalMCPCandidateDto {
  id: string;
  search_id: string;
  source_id: string;
  external_key: string;
  name: string;
  description: string | null;
  version: string | null;
  license: string | null;
  repository_url: string | null;
  homepage_url: string | null;
  transport_type: string | null;
  endpoint_url: string | null;
  review_state: ExternalMCPReviewState;
  imported_mcp_server_id: string | null;
  discovered_at: string;
}

export interface ExternalMCPSearchDto {
  id: string;
  source_id: string;
  query: string;
  status: ExternalMCPSearchStatus;
  requested_limit: number;
  candidate_count: number;
  error_code: string | null;
  error_message: string | null;
  requested_by: string;
  started_at: string;
  finished_at: string | null;
  candidates: ExternalMCPCandidateDto[];
}

export interface ExternalMCPReviewCreateRequest {
  decision: ExternalMCPReviewDecision;
  comment?: string | null;
}

export interface ExternalMCPReviewDto {
  id: string;
  candidate_id: string;
  decision: ExternalMCPReviewDecision;
  comment: string | null;
  reviewed_by: string;
  reviewed_at: string;
  review_state: ExternalMCPReviewState;
}

export interface ExternalMCPImportDto {
  candidate_id: string;
  mcp_server_id: string;
  created: boolean;
  server_status: MCPServerStatus;
  transport_type: MCPTransportType | null;
}
