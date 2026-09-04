import type {
  ExecutionSourceType,
  MCPAuthType,
  MCPCheckStatus,
  MCPDiscoveryMode,
  MCPProtocolEra,
  MCPTransportType,
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

export const MCP_TRANSPORT_LABELS: Record<MCPTransportType, string> = {
  STREAMABLE_HTTP: 'Streamable HTTP',
  STDIO: 'STDIO',
  LEGACY_HTTP_SSE: 'Legacy HTTP/SSE',
};

export const MCP_PROTOCOL_ERA_LABELS: Record<MCPProtocolEra, string> = {
  CURRENT: 'Current MCP',
  LEGACY: 'Legacy MCP',
};

export const MCP_CHECK_STATUS_LABELS: Record<MCPCheckStatus, string> = {
  SUCCEEDED: 'Succeeded',
  FAILED: 'Failed',
  TIMED_OUT: 'Timed Out',
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

export function labelTransport(value: string): string {
  return MCP_TRANSPORT_LABELS[value as MCPTransportType] ?? value;
}

export function labelProtocolEra(value: string): string {
  return MCP_PROTOCOL_ERA_LABELS[value as MCPProtocolEra] ?? value;
}

export function labelCheckStatus(value: string): string {
  return MCP_CHECK_STATUS_LABELS[value as MCPCheckStatus] ?? value;
}

/** Format Backend ISO-8601 timestamps for presentation only. */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function shortenId(value: string | null | undefined, keep = 8): string {
  if (!value) return '—';
  if (value.length <= keep + 1) return value;
  return `${value.slice(0, keep)}…`;
}
