import type {
  ExecutionSourceType,
  MCPAuthType,
  MCPDiscoveryMode,
  ScheduleTargetType,
} from './types';

/** Presentation labels — Domain values stay Canonical; UI maps separately. */

export const EXECUTION_SOURCE_LABELS: Record<ExecutionSourceType, string> = {
  AGENT_REQUEST: 'Agent',
  WORKFLOW_VERSION: 'Workflow',
  SCHEDULE_OCCURRENCE: 'Schedule',
  MANUAL_TOOL_TEST: 'Manual Tool Test',
  FACTORY_TEST: 'Factory Test',
};

export const SCHEDULE_TARGET_LABELS: Record<ScheduleTargetType, string> = {
  AGENT_VERSION: 'Agent Version',
  WORKFLOW_VERSION: 'Workflow Version',
};

export const MCP_DISCOVERY_LABELS: Record<MCPDiscoveryMode, string> = {
  EXPLICIT_DISCOVERY: 'Explicit Discovery',
  INFERRED_CURRENT: 'Inferred Current',
  LEGACY_HANDSHAKE: 'Legacy Handshake',
};

export const MCP_AUTH_LABELS: Record<MCPAuthType, string> = {
  NONE: 'None',
  BEARER: 'Bearer Token',
  API_KEY_HEADER: 'API Key Header',
  BASIC: 'Basic',
  OAUTH2: 'OAuth 2.0',
  CUSTOM_HEADERS: 'Custom Headers',
  STDIO_ENV: 'STDIO Env',
};

export function labelExecutionSource(value: string): string {
  return EXECUTION_SOURCE_LABELS[value as ExecutionSourceType] ?? value;
}

export function labelScheduleTarget(value: string): string {
  return SCHEDULE_TARGET_LABELS[value as ScheduleTargetType] ?? value;
}

export function labelDiscoveryMode(value: string): string {
  return MCP_DISCOVERY_LABELS[value as MCPDiscoveryMode] ?? value;
}

export function labelAuthType(value: string): string {
  return MCP_AUTH_LABELS[value as MCPAuthType] ?? value;
}
