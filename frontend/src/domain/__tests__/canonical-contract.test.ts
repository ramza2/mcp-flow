/**
 * Canonical Contract Tests — docs/04, docs/05, AGENTS.md
 * Exact Domain enum sets must stay aligned with design docs.
 */
import { describe, expect, it } from 'vitest';
import {
  AGENT_REQUEST_STATUSES,
  AGENT_STATUSES,
  AGENT_VERSION_STATUSES,
  APPROVAL_STATUSES,
  AUTHORABLE_STEP_TYPES,
  CURRENT_MCP_PROTOCOL_VERSION,
  EXECUTION_SOURCE_TYPES,
  EXECUTION_STATUSES,
  JOB_STATUSES,
  MCP_AUTH_TYPES,
  MCP_CHECK_STATUSES,
  MCP_CHECK_TYPES,
  MCP_DISCOVERY_MODES,
  MCP_PROTOCOL_ERAS,
  MCP_SERVER_STATUSES,
  MCP_TRANSPORT_TYPES,
  MCP_TOOL_STATUSES,
  OCCURRENCE_STATUSES,
  RISK_CLASSES,
  SCHEDULE_MISFIRE_POLICIES,
  SCHEDULE_OVERLAP_POLICIES,
  SCHEDULE_STATUSES,
  SCHEDULE_TARGET_TYPES,
  STEP_STATUSES,
  TOOL_VERIFICATION_STATUSES,
  TOOL_VERSION_VALIDATION_STATUSES,
  WORKFLOW_STATUSES,
  WORKFLOW_VERSION_STATUSES,
} from '../types';
import {
  labelDiscoveryMode,
  labelExecutionSource,
  labelScheduleTarget,
} from '../labels';

function expectExact(actual: readonly string[], expected: readonly string[]) {
  expect([...actual].sort()).toEqual([...expected].sort());
  expect(actual).toHaveLength(expected.length);
  for (const value of expected) {
    expect(actual).toContain(value);
  }
}

describe('Canonical Domain Contract (docs/04/05)', () => {
  it('MCPServerStatus', () => {
    expectExact(MCP_SERVER_STATUSES, ['DRAFT', 'ACTIVE', 'INACTIVE', 'ERROR']);
  });

  it('MCPToolStatus', () => {
    expectExact(MCP_TOOL_STATUSES, ['DISCOVERED', 'ACTIVE', 'INACTIVE', 'MISSING', 'BLOCKED']);
  });

  it('ToolVersionValidation', () => {
    expectExact(TOOL_VERSION_VALIDATION_STATUSES, ['VALID', 'INVALID', 'WARNING']);
  });

  it('ToolVerification', () => {
    expectExact(TOOL_VERIFICATION_STATUSES, ['PENDING', 'VERIFIED', 'FAILED', 'EXPIRED']);
  });

  it('Agent logical status', () => {
    expectExact(AGENT_STATUSES, ['DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED']);
  });

  it('AgentVersion', () => {
    expectExact(AGENT_VERSION_STATUSES, ['DRAFT', 'PUBLISHED', 'DEPRECATED']);
  });

  it('Workflow logical status', () => {
    expectExact(WORKFLOW_STATUSES, ['DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED']);
  });

  it('WorkflowVersion', () => {
    expectExact(WORKFLOW_VERSION_STATUSES, ['DRAFT', 'PUBLISHED', 'DEPRECATED']);
  });

  it('AgentRequest', () => {
    expectExact(AGENT_REQUEST_STATUSES, [
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
    ]);
  });

  it('Execution', () => {
    expectExact(EXECUTION_STATUSES, [
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
    ]);
  });

  it('Step', () => {
    expectExact(STEP_STATUSES, [
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
    ]);
  });

  it('Approval', () => {
    expectExact(APPROVAL_STATUSES, ['PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED']);
  });

  it('Job', () => {
    expectExact(JOB_STATUSES, ['PENDING', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT']);
  });

  it('Schedule', () => {
    expectExact(SCHEDULE_STATUSES, ['ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR']);
  });

  it('Occurrence', () => {
    expectExact(OCCURRENCE_STATUSES, ['PLANNED', 'SKIPPED', 'ENQUEUED', 'RUNNING', 'COMPLETED', 'FAILED']);
  });

  it('risk_class', () => {
    expectExact(RISK_CLASSES, [
      'READ_ONLY',
      'IDEMPOTENT_WRITE',
      'NON_IDEMPOTENT_WRITE',
      'DESTRUCTIVE',
      'UNKNOWN',
    ]);
  });

  it('ExecutionSource', () => {
    expectExact(EXECUTION_SOURCE_TYPES, [
      'AGENT_REQUEST',
      'WORKFLOW_VERSION',
      'SCHEDULE_OCCURRENCE',
      'MANUAL_TOOL_TEST',
      'FACTORY_TEST',
    ]);
  });

  it('ScheduleTarget', () => {
    expectExact(SCHEDULE_TARGET_TYPES, ['AGENT_VERSION', 'WORKFLOW_VERSION']);
  });

  it('Overlap', () => {
    expectExact(SCHEDULE_OVERLAP_POLICIES, ['ALLOW', 'SKIP', 'QUEUE', 'REPLACE']);
  });

  it('Misfire', () => {
    expectExact(SCHEDULE_MISFIRE_POLICIES, ['SKIP', 'RUN_ONCE', 'CATCH_UP_LIMITED']);
  });

  it('MCPDiscovery', () => {
    expectExact(MCP_DISCOVERY_MODES, ['EXPLICIT_DISCOVERY', 'INFERRED_CURRENT', 'LEGACY_HANDSHAKE']);
  });

  it('MCPAuth', () => {
    expectExact(MCP_AUTH_TYPES, [
      'NONE',
      'BEARER',
      'API_KEY_HEADER',
      'BASIC',
      'OAUTH2',
      'CUSTOM_HEADERS',
      'STDIO_ENV',
    ]);
  });

  it('MCPTransportType', () => {
    expectExact(MCP_TRANSPORT_TYPES, ['STDIO', 'STREAMABLE_HTTP', 'LEGACY_HTTP_SSE']);
  });

  it('MCPProtocolEra', () => {
    expectExact(MCP_PROTOCOL_ERAS, ['CURRENT', 'LEGACY']);
  });

  it('MCPCheckType', () => {
    expectExact(MCP_CHECK_TYPES, ['MANUAL', 'SCHEDULED', 'PRE_ACTIVATION']);
  });

  it('MCPCheckStatus', () => {
    expectExact(MCP_CHECK_STATUSES, ['SUCCEEDED', 'FAILED', 'TIMED_OUT']);
  });

  it('Current MCP protocol version', () => {
    expect(CURRENT_MCP_PROTOCOL_VERSION).toBe('2026-07-28');
  });

  it('Authorable Plan Step Types (persisted)', () => {
    expectExact(AUTHORABLE_STEP_TYPES, ['TOOL', 'CONDITION', 'JOIN', 'APPROVAL', 'LOOP']);
  });
});

describe('Negative Canonical Contract — forbidden Domain values', () => {
  it('Execution must not include planning/approval-entity statuses', () => {
    for (const bad of ['PLANNING', 'WAITING_CONFIRMATION', 'REJECTED', 'EXPIRED', 'PARTIAL'] as const) {
      expect(EXECUTION_STATUSES).not.toContain(bad);
    }
  });

  it('Schedule must not include removed policies/statuses', () => {
    expect(SCHEDULE_STATUSES).not.toContain('INACTIVE');
    expect(SCHEDULE_OVERLAP_POLICIES).not.toContain('CANCEL_RUNNING');
    expect(SCHEDULE_MISFIRE_POLICIES).not.toContain('RUN_ALL');
  });

  it('Logical Workflow must not use PUBLISHED', () => {
    expect(WORKFLOW_STATUSES).not.toContain('PUBLISHED');
    expect(AGENT_STATUSES).not.toContain('PUBLISHED');
  });

  it('Approval must not use WAITING_APPROVAL', () => {
    expect(APPROVAL_STATUSES).not.toContain('WAITING_APPROVAL');
  });

  it('Persisted Plan Step Types must not include visual/script types', () => {
    for (const bad of ['USER_INPUT', 'PARALLEL', 'END', 'SCRIPT', 'PYTHON', 'JAVASCRIPT'] as const) {
      expect(AUTHORABLE_STEP_TYPES).not.toContain(bad);
    }
  });

  it('MCPServerStatus must not include CONNECTED or transport labels', () => {
    for (const bad of ['CONNECTED', 'DISCONNECTED', 'STREAMABLE_HTTP', 'STDIO'] as const) {
      expect(MCP_SERVER_STATUSES).not.toContain(bad);
    }
    expect(MCP_TRANSPORT_TYPES).not.toContain('Streamable HTTP');
    expect(MCP_TRANSPORT_TYPES).not.toContain('CONNECTED');
  });
});

describe('Presentation labels — Domain values stay Canonical', () => {
  it('maps Execution source Canonical → label without replacing Domain keys', () => {
    expect(labelExecutionSource('AGENT_REQUEST')).toBe('Agent');
    expect(labelExecutionSource('WORKFLOW_VERSION')).toBe('Workflow');
    expect(labelExecutionSource('SCHEDULE_OCCURRENCE')).toBe('Schedule');
    expect(EXECUTION_SOURCE_TYPES).toContain('AGENT_REQUEST');
    expect(EXECUTION_SOURCE_TYPES).not.toContain('Agent');
  });

  it('maps Schedule target Canonical → label', () => {
    expect(labelScheduleTarget('AGENT_VERSION')).toBe('Agent Version');
    expect(labelScheduleTarget('WORKFLOW_VERSION')).toBe('Workflow Version');
  });

  it('maps MCP discovery Canonical → label', () => {
    expect(labelDiscoveryMode('INFERRED_CURRENT')).toBe('Inferred Current');
    expect(labelDiscoveryMode('LEGACY_HANDSHAKE')).toBe('Legacy Handshake');
  });
});
