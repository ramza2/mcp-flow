import type {
  AgentStatus,
  AgentVersionStatus,
  AgentVisibility,
  AuditActorType,
  AuditResult,
  ExecutionSourceType,
  ExecutionTriggerType,
  ExternalMCPReviewState,
  MCPAuthType,
  MCPCheckStatus,
  MCPDiscoveryMode,
  MCPProtocolEra,
  MCPTransportType,
  RiskClass,
  OccurrenceStatus,
  ScheduleMisfirePolicy,
  ScheduleOverlapPolicy,
  ScheduleStatus,
  ScheduleTargetType,
} from './types';

/** Presentation labels — Domain values stay Canonical; UI maps separately. */

export const AGENT_STATUS_LABELS: Record<AgentStatus, string> = {
  DRAFT: '초안',
  ACTIVE: '활성',
  INACTIVE: '비활성',
  ARCHIVED: '보관됨',
};

export const AGENT_VERSION_STATUS_LABELS: Record<AgentVersionStatus, string> = {
  DRAFT: '초안',
  PUBLISHED: '게시됨',
  DEPRECATED: '이전 버전',
};

export const AGENT_VISIBILITY_LABELS: Record<AgentVisibility, string> = {
  PRIVATE: '비공개 (나만 보기)',
  RESTRICTED: '제한 공개',
  INTERNAL: '조직 내부 공개',
};

export function labelAgentStatus(value: string): string {
  return AGENT_STATUS_LABELS[value as AgentStatus] ?? value;
}

export function labelAgentVersionStatus(value: string): string {
  return AGENT_VERSION_STATUS_LABELS[value as AgentVersionStatus] ?? value;
}

export function labelAgentVisibility(value: string): string {
  return AGENT_VISIBILITY_LABELS[value as AgentVisibility] ?? value;
}

export const EXECUTION_SOURCE_LABELS: Record<ExecutionSourceType, string> = {
  AGENT_REQUEST: 'Agent',
  WORKFLOW_VERSION: 'Workflow',
  SCHEDULE_OCCURRENCE: 'Schedule',
  MANUAL_TOOL_TEST: 'Manual Tool Test',
  FACTORY_TEST: 'Factory Test',
};

export const EXECUTION_TRIGGER_LABELS: Record<ExecutionTriggerType, string> = {
  USER: 'User',
  SCHEDULE: 'Schedule',
  RETRY: 'Retry',
  SYSTEM: 'System',
  TEST: 'Test',
};

export const AUDIT_ACTOR_TYPE_LABELS: Record<AuditActorType, string> = {
  USER: 'User',
  SERVICE: 'Service',
  SYSTEM: 'System',
};

export const AUDIT_RESULT_LABELS: Record<AuditResult, string> = {
  SUCCESS: 'Success',
  DENIED: 'Denied',
  FAILURE: 'Failure',
};

export const SCHEDULE_TARGET_LABELS: Record<ScheduleTargetType, string> = {
  AGENT_VERSION: '에이전트 버전',
  WORKFLOW_VERSION: '워크플로 버전',
};

export const SCHEDULE_STATUS_LABELS: Record<ScheduleStatus, string> = {
  ACTIVE: '활성화됨',
  PAUSED: '일시정지',
  COMPLETED: '실행 완료',
  ERROR: '오류',
};

export const OCCURRENCE_STATUS_LABELS: Record<OccurrenceStatus, string> = {
  PLANNED: '실행 예정',
  SKIPPED: '건너뜀',
  ENQUEUED: '실행 대기열 등록',
  RUNNING: '실행 중',
  COMPLETED: '완료',
  FAILED: '실패',
};

export const SCHEDULE_OVERLAP_LABELS: Record<ScheduleOverlapPolicy, string> = {
  ALLOW: '동시 실행 허용',
  SKIP: '이전 실행 중이면 건너뛰기 (기본값)',
  QUEUE: '이전 실행 완료 후 대기 작업 실행',
  REPLACE: '이전 실행 취소 요청 후 교체',
};

export const SCHEDULE_MISFIRE_LABELS: Record<ScheduleMisfirePolicy, string> = {
  SKIP: '놓친 실행은 건너뛰기 (기본값)',
  RUN_ONCE: '놓친 실행이 있으면 한 번 실행',
  CATCH_UP_LIMITED: '놓친 실행을 지정 횟수까지 보충',
};

/** Scheduler decision reasons have no frontend canonical enum; unknown values remain visible. */
const SCHEDULE_DECISION_LABELS: Record<string, string> = {
  DUE: '예정된 시각에 실행',
  MANUAL_TRIGGER: '수동 실행',
  MISFIRE_SKIP: '놓친 실행 시각 건너뛰기',
  MISFIRE_RUN_ONCE: '놓친 실행 한 번 실행',
  MISFIRE_CATCH_UP: '놓친 실행 보충',
  MISFIRE_COALESCED: '놓친 실행 합치기',
  MISFIRE_CATCH_UP_LIMIT: '보충 실행 횟수 초과',
  OVERLAP_SKIP: '이전 실행 중이어서 건너뛰기',
  OVERLAP_QUEUE: '이전 실행 완료까지 대기',
  OVERLAP_REPLACE: '이전 실행 교체',
  OVERLAP_REPLACE_WAIT: '이전 실행 취소 대기',
  REPLACE_SUPERSEDED: '더 최근 실행으로 대체됨',
  TARGET_PRECONDITION_FAILED: '실행 대상 조건 미충족',
  AUTHORIZATION_REVOKED: '실행 권한 철회됨',
  AGENT_SCHEDULE_EXECUTION_UNSUPPORTED: '지원되지 않는 에이전트 예약 실행',
  AGENT_PLAN_QUEUED: '에이전트 실행 계획 대기',
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

export const EXTERNAL_MCP_REVIEW_STATE_LABELS: Record<ExternalMCPReviewState, string> = {
  UNREVIEWED: 'Unreviewed',
  APPROVED: 'Approved',
  REJECTED: 'Rejected',
};

export function labelExternalMCPReviewState(value: string): string {
  return EXTERNAL_MCP_REVIEW_STATE_LABELS[value as ExternalMCPReviewState] ?? value;
}

export const RISK_CLASS_LABELS: Record<RiskClass, string> = {
  READ_ONLY: 'Read only',
  IDEMPOTENT_WRITE: 'Idempotent write',
  NON_IDEMPOTENT_WRITE: 'Non-idempotent write',
  DESTRUCTIVE: 'Destructive',
  UNKNOWN: 'Unknown',
};

export function labelExecutionSource(value: string): string {
  return EXECUTION_SOURCE_LABELS[value as ExecutionSourceType] ?? value;
}

export function labelExecutionTrigger(value: string): string {
  return EXECUTION_TRIGGER_LABELS[value as ExecutionTriggerType] ?? value;
}

export function labelAuditActorType(value: string): string {
  return AUDIT_ACTOR_TYPE_LABELS[value as AuditActorType] ?? value;
}

export function labelAuditResult(value: string): string {
  return AUDIT_RESULT_LABELS[value as AuditResult] ?? value;
}

/** Format duration_ms from safe Execution/Step projections. */
export function formatDurationMs(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return `${minutes}m ${rem}s`;
}

export function labelScheduleTarget(value: string): string {
  return SCHEDULE_TARGET_LABELS[value as ScheduleTargetType] ?? value;
}

export function labelScheduleStatus(value: string): string {
  return SCHEDULE_STATUS_LABELS[value as ScheduleStatus] ?? value;
}

export function labelOccurrenceStatus(value: string): string {
  return OCCURRENCE_STATUS_LABELS[value as OccurrenceStatus] ?? value;
}

export function labelScheduleOverlap(value: string): string {
  return SCHEDULE_OVERLAP_LABELS[value as ScheduleOverlapPolicy] ?? value;
}

export function labelScheduleMisfire(value: string): string {
  return SCHEDULE_MISFIRE_LABELS[value as ScheduleMisfirePolicy] ?? value;
}

export function labelScheduleDecision(value: string): string {
  return SCHEDULE_DECISION_LABELS[value] ?? value;
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

export function labelRiskClass(value: string): string {
  return RISK_CLASS_LABELS[value as RiskClass] ?? value;
}

/** Format Backend ISO-8601 timestamps for presentation only. */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Schedule dates must be shown in the Schedule's IANA timezone, not the browser's. */
export function formatScheduleTimestamp(value: string | null | undefined, timezone: string): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try {
    const formatted = new Intl.DateTimeFormat('sv-SE', {
      timeZone: timezone,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(date);
    return `${formatted} (${timezone})`;
  } catch {
    return formatTimestamp(value);
  }
}

export function shortenId(value: string | null | undefined, keep = 8): string {
  if (!value) return '—';
  if (value.length <= keep + 1) return value;
  return `${value.slice(0, keep)}…`;
}
