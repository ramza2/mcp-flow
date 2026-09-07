/** API DTO fixtures for MCP registry tests — aligned with backend schemas. */

import type {
  ConnectionTestDto,
  DiscoveryDto,
  DiscoveryListDto,
  MCPServerDto,
  MCPServerListDto,
  MCPToolDto,
  MCPToolListDto,
  MCPToolPolicyDto,
  MCPToolVersionDto,
  MCPToolVersionListDto,
  ToolVerificationDto,
  ToolVerificationListDto,
} from '../../src/api/types';

export const draftServer: MCPServerDto = {
  id: 'srv-draft-001',
  code: 'weather-mcp',
  name: 'Weather MCP',
  description: 'Weather data provider',
  transport_type: 'STREAMABLE_HTTP',
  endpoint_url: 'https://mcp.example.com/mcp',
  stdio_manifest_id: null,
  auth_type: 'NONE',
  auth_secret_id: null,
  status: 'DRAFT',
  protocol_era: 'CURRENT',
  discovery_mode: null,
  negotiated_protocol_version: null,
  capabilities: null,
  connect_timeout_ms: 5000,
  call_timeout_ms: 30000,
  max_concurrency: 4,
  last_healthy_at: null,
  last_error_at: null,
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-02T14:00:00Z',
  lock_version: 1,
};

export const activeServer: MCPServerDto = {
  ...draftServer,
  id: 'srv-active-001',
  code: 'docs-mcp',
  name: 'Docs MCP',
  status: 'ACTIVE',
  discovery_mode: 'INFERRED_CURRENT',
  negotiated_protocol_version: '2026-07-28',
  last_healthy_at: '2026-09-02T14:20:00Z',
  updated_at: '2026-09-02T14:20:00Z',
  lock_version: 2,
};

export const serverList: MCPServerListDto = {
  items: [activeServer, draftServer],
  page: 1,
  page_size: 20,
  total: 2,
  has_next: false,
};

export const discoveredTool: MCPToolDto = {
  id: 'tool-disc-001',
  mcp_server_id: activeServer.id,
  remote_name: 'search_docs',
  display_name: 'Search Docs',
  description_override: null,
  tags: null,
  status: 'DISCOVERED',
  current_version_id: 'ver-valid-001',
  first_seen_at: '2026-09-01T10:00:00Z',
  last_seen_at: '2026-09-02T14:00:00Z',
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-02T14:00:00Z',
  lock_version: 1,
};

export const missingTool: MCPToolDto = {
  ...discoveredTool,
  id: 'tool-miss-001',
  remote_name: 'old_tool',
  display_name: null,
  status: 'MISSING',
  current_version_id: null,
};

export const toolList: MCPToolListDto = {
  items: [discoveredTool, missingTool],
  page: 1,
  page_size: 20,
  total: 2,
  has_next: false,
};

export const validVersion: MCPToolVersionDto = {
  id: 'ver-valid-001',
  mcp_tool_id: discoveredTool.id,
  version_no: 1,
  remote_description: 'Search documentation',
  input_schema: {
    type: 'object',
    properties: { query: { type: 'string' } },
    required: ['query'],
  },
  output_schema: {
    type: 'object',
    properties: { results: { type: 'array' } },
  },
  annotations: null,
  schema_dialect: 'json-schema',
  content_hash: 'abc123',
  validation_status: 'VALID',
  validation_errors: null,
  discovered_at: '2026-09-01T10:00:00Z',
  created_at: '2026-09-01T10:00:00Z',
};

export const invalidVersion: MCPToolVersionDto = {
  ...validVersion,
  id: 'ver-invalid-001',
  version_no: 2,
  validation_status: 'INVALID',
  validation_errors: [{ path: 'input_schema', message: 'missing type' }],
};

export const warningVersion: MCPToolVersionDto = {
  ...validVersion,
  id: 'ver-warn-001',
  version_no: 3,
  validation_status: 'WARNING',
  validation_errors: [{ path: 'output_schema', message: 'optional field untyped' }],
};

export const versionList: MCPToolVersionListDto = {
  items: [validVersion, warningVersion],
  page: 1,
  page_size: 20,
  total: 2,
  has_next: false,
};

export const succeededCheck: ConnectionTestDto = {
  id: 'chk-001',
  mcp_server_id: activeServer.id,
  check_type: 'MANUAL',
  status: 'SUCCEEDED',
  latency_ms: 42,
  protocol_version: '2026-07-28',
  discovery_mode: 'INFERRED_CURRENT',
  error_layer: null,
  error_code: null,
  error_message: null,
  checked_at: '2026-09-02T14:20:00Z',
};

export const failedCheck: ConnectionTestDto = {
  ...succeededCheck,
  id: 'chk-002',
  status: 'FAILED',
  latency_ms: null,
  protocol_version: null,
  error_layer: 'TRANSPORT',
  error_code: 'CONNECTION_REFUSED',
  error_message: 'Connection refused',
};

export const timedOutCheck: ConnectionTestDto = {
  ...succeededCheck,
  id: 'chk-003',
  status: 'TIMED_OUT',
  latency_ms: 5000,
  error_message: 'Request timed out',
};

export const previewDiscovery: DiscoveryDto = {
  id: 'disc-preview-001',
  mcp_server_id: activeServer.id,
  protocol_era: 'CURRENT',
  discovery_mode: 'INFERRED_CURRENT',
  selected_version: '2026-07-28',
  success: true,
  error_code: null,
  error_message: null,
  apply_changes: false,
  diff: { added: 1, changed: 0, missing: 0, unchanged: 2 },
  started_at: '2026-09-02T14:30:00Z',
  finished_at: '2026-09-02T14:30:05Z',
  capabilities: null,
};

export const appliedDiscovery: DiscoveryDto = {
  ...previewDiscovery,
  id: 'disc-applied-001',
  apply_changes: true,
  started_at: '2026-09-02T10:00:00Z',
  finished_at: '2026-09-02T10:00:08Z',
};

export const discoveryList: DiscoveryListDto = {
  items: [appliedDiscovery, previewDiscovery],
  page: 1,
  page_size: 20,
  total: 2,
  has_next: false,
};

export const activeTool: MCPToolDto = {
  ...discoveredTool,
  id: 'tool-active-001',
  status: 'ACTIVE',
  lock_version: 2,
  tags: ['docs', 'search'],
};

export const inactiveTool: MCPToolDto = {
  ...discoveredTool,
  id: 'tool-inactive-001',
  status: 'INACTIVE',
  lock_version: 3,
};

export const blockedTool: MCPToolDto = {
  ...discoveredTool,
  id: 'tool-blocked-001',
  status: 'BLOCKED',
  lock_version: 1,
};

export const toolPolicy: MCPToolPolicyDto = {
  id: 'pol-001',
  mcp_tool_id: discoveredTool.id,
  risk_class: 'READ_ONLY',
  requires_confirmation: false,
  requires_approval: false,
  approval_policy_id: null,
  timeout_ms: 30000,
  max_attempts: 1,
  backoff_policy: null,
  max_result_bytes: 1048576,
  allow_auto_select: true,
  data_classification: null,
  policy_metadata: null,
  updated_at: '2026-09-02T15:00:00Z',
  updated_by: null,
  lock_version: 1,
};

export const toolPolicyWithApproval: MCPToolPolicyDto = {
  ...toolPolicy,
  id: 'pol-002',
  requires_approval: true,
  approval_policy_id: 'ap-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
  risk_class: 'DESTRUCTIVE',
  lock_version: 2,
};

export const pendingVerification: ToolVerificationDto = {
  id: 'verif-pending-001',
  mcp_tool_version_id: validVersion.id,
  status: 'PENDING',
  verified_by: null,
  verified_at: '2026-09-02T16:00:00Z',
  test_execution_id: null,
  criteria_version: 'tool-verification-v1',
  result_summary: null,
  evidence_blob_id: null,
  expires_at: null,
};

export const failedVerification: ToolVerificationDto = {
  id: 'verif-failed-001',
  mcp_tool_version_id: validVersion.id,
  status: 'FAILED',
  verified_by: null,
  verified_at: '2026-09-02T16:05:00Z',
  test_execution_id: null,
  criteria_version: 'tool-verification-v1',
  result_summary: { schema_valid: false, reason: 'Manual review failed' },
  evidence_blob_id: null,
  expires_at: null,
};

export const verifiedVerification: ToolVerificationDto = {
  id: 'verif-ok-001',
  mcp_tool_version_id: validVersion.id,
  status: 'VERIFIED',
  verified_by: null,
  verified_at: '2026-09-02T16:10:00Z',
  test_execution_id: 'exec-11111111-2222-3333-4444-555555555555',
  criteria_version: 'tool-verification-v1',
  result_summary: {
    schema_valid: true,
    normal_call_passed: true,
    error_handling_checked: true,
  },
  evidence_blob_id: 'blob-11111111-2222-3333-4444-555555555555',
  expires_at: '2027-09-02T16:10:00Z',
};

export const expiredVerification: ToolVerificationDto = {
  ...verifiedVerification,
  id: 'verif-expired-001',
  status: 'EXPIRED',
  expires_at: '2026-01-01T00:00:00Z',
};

export const verificationList: ToolVerificationListDto = {
  items: [pendingVerification, failedVerification, verifiedVerification, expiredVerification],
  page: 1,
  page_size: 20,
  total: 4,
  has_next: false,
};

export const verificationListV2: ToolVerificationListDto = {
  items: [],
  page: 1,
  page_size: 20,
  total: 0,
  has_next: false,
};
