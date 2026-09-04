/** MCPFlow Canonical Domain Types — source: docs/05-data-model.md */

export const MCP_SERVER_STATUSES = ['DRAFT', 'ACTIVE', 'INACTIVE', 'ERROR'] as const;
export type MCPServerStatus = (typeof MCP_SERVER_STATUSES)[number];

export const MCP_TOOL_STATUSES = ['DISCOVERED', 'ACTIVE', 'INACTIVE', 'MISSING', 'BLOCKED'] as const;
export type MCPToolStatus = (typeof MCP_TOOL_STATUSES)[number];

export const TOOL_VERSION_VALIDATION_STATUSES = ['VALID', 'INVALID', 'WARNING'] as const;
export type ToolVersionValidationStatus = (typeof TOOL_VERSION_VALIDATION_STATUSES)[number];

export const TOOL_VERIFICATION_STATUSES = ['PENDING', 'VERIFIED', 'FAILED', 'EXPIRED'] as const;
export type ToolVerificationStatus = (typeof TOOL_VERIFICATION_STATUSES)[number];

export const AGENT_STATUSES = ['DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED'] as const;
export type AgentStatus = (typeof AGENT_STATUSES)[number];

export const AGENT_VERSION_STATUSES = ['DRAFT', 'PUBLISHED', 'DEPRECATED'] as const;
export type AgentVersionStatus = (typeof AGENT_VERSION_STATUSES)[number];

export const WORKFLOW_STATUSES = ['DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED'] as const;
export type WorkflowStatus = (typeof WORKFLOW_STATUSES)[number];

export const WORKFLOW_VERSION_STATUSES = ['DRAFT', 'PUBLISHED', 'DEPRECATED'] as const;
export type WorkflowVersionStatus = (typeof WORKFLOW_VERSION_STATUSES)[number];

export const AGENT_REQUEST_STATUSES = [
  'RECEIVED',
  'ANALYZING',
  'RETRIEVING',
  'SELECTING',
  'BUILDING_PARAMETERS',
  'PLANNING',
  'VALIDATING',
  'WAITING_INPUT',
  'WAITING_CONFIRMATION',
  'READY',
  'REJECTED',
  'FAILED',
  'CANCELLED',
] as const;
export type AgentRequestStatus = (typeof AGENT_REQUEST_STATUSES)[number];

export const EXECUTION_STATUSES = [
  'CREATED',
  'QUEUED',
  'RUNNING',
  'WAITING_INPUT',
  'WAITING_APPROVAL',
  'CANCEL_REQUESTED',
  'SUCCEEDED',
  'PARTIALLY_SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'TIMED_OUT',
] as const;
export type ExecutionStatus = (typeof EXECUTION_STATUSES)[number];

export const STEP_STATUSES = [
  'PENDING',
  'READY',
  'RUNNING',
  'WAITING_INPUT',
  'WAITING_APPROVAL',
  'SUCCEEDED',
  'FAILED',
  'SKIPPED',
  'TIMED_OUT',
  'CANCELLED',
  'UNKNOWN_OUTCOME',
] as const;
export type StepStatus = (typeof STEP_STATUSES)[number];

export const APPROVAL_STATUSES = ['PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED'] as const;
export type ApprovalStatus = (typeof APPROVAL_STATUSES)[number];

export const JOB_STATUSES = ['PENDING', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT'] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const SCHEDULE_STATUSES = ['ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR'] as const;
export type ScheduleStatus = (typeof SCHEDULE_STATUSES)[number];

export const OCCURRENCE_STATUSES = ['PLANNED', 'SKIPPED', 'ENQUEUED', 'RUNNING', 'COMPLETED', 'FAILED'] as const;
export type OccurrenceStatus = (typeof OCCURRENCE_STATUSES)[number];

export const RISK_CLASSES = [
  'READ_ONLY',
  'IDEMPOTENT_WRITE',
  'NON_IDEMPOTENT_WRITE',
  'DESTRUCTIVE',
  'UNKNOWN',
] as const;
export type RiskClass = (typeof RISK_CLASSES)[number];

export const EXECUTION_SOURCE_TYPES = [
  'AGENT_REQUEST',
  'WORKFLOW_VERSION',
  'SCHEDULE_OCCURRENCE',
  'MANUAL_TOOL_TEST',
  'FACTORY_TEST',
] as const;
export type ExecutionSourceType = (typeof EXECUTION_SOURCE_TYPES)[number];

export const SCHEDULE_TARGET_TYPES = ['AGENT_VERSION', 'WORKFLOW_VERSION'] as const;
export type ScheduleTargetType = (typeof SCHEDULE_TARGET_TYPES)[number];

export const SCHEDULE_OVERLAP_POLICIES = ['ALLOW', 'SKIP', 'QUEUE', 'REPLACE'] as const;
export type ScheduleOverlapPolicy = (typeof SCHEDULE_OVERLAP_POLICIES)[number];

export const SCHEDULE_MISFIRE_POLICIES = ['SKIP', 'RUN_ONCE', 'CATCH_UP_LIMITED'] as const;
export type ScheduleMisfirePolicy = (typeof SCHEDULE_MISFIRE_POLICIES)[number];

export const MCP_DISCOVERY_MODES = ['EXPLICIT_DISCOVERY', 'INFERRED_CURRENT', 'LEGACY_HANDSHAKE'] as const;
export type MCPDiscoveryMode = (typeof MCP_DISCOVERY_MODES)[number];

export const MCP_AUTH_TYPES = [
  'NONE',
  'BEARER',
  'API_KEY_HEADER',
  'BASIC',
  'OAUTH2',
  'CUSTOM_HEADERS',
  'STDIO_ENV',
] as const;
export type MCPAuthType = (typeof MCP_AUTH_TYPES)[number];

/** Authorable Execution Plan v1 Step Types (docs/04). Visual PARALLEL/END are not persisted. */
export const AUTHORABLE_STEP_TYPES = ['TOOL', 'CONDITION', 'JOIN', 'APPROVAL', 'LOOP'] as const;
export type AuthorableStepType = (typeof AUTHORABLE_STEP_TYPES)[number];

/** Current MCP protocol version (docs/04). */
export const CURRENT_MCP_PROTOCOL_VERSION = '2026-07-28';
