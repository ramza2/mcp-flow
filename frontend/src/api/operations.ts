/**
 * Operations Dashboard / stats / system-health — docs/06 §18 / PR #58.
 * Aggregate projections only; no MCP Server/Tool names from this API.
 */

import { apiRequest } from './client';
import type { ExecutionListItemDto } from './executions';

export interface ExecutionStatusCountsDto {
  total: number;
  created: number;
  queued: number;
  running: number;
  waiting_input: number;
  waiting_approval: number;
  cancel_requested: number;
  succeeded: number;
  partially_succeeded: number;
  failed: number;
  cancelled: number;
  timed_out: number;
}

export interface ApprovalOpsSummaryDto {
  pending: number;
  overdue: number;
}

export interface ScheduleOpsSummaryDto {
  active: number;
  paused: number;
  completed: number;
  error: number;
  overdue: number;
}

export interface MCPServerOpsSummaryDto {
  total: number;
  active: number;
  inactive: number;
  error: number;
  draft: number;
}

export interface MCPToolOpsSummaryDto {
  total: number;
  discovered: number;
  active: number;
  inactive: number;
  missing: number;
  blocked: number;
  problematic: number;
}

export interface DashboardSummaryDto {
  window_from: string;
  window_to: string;
  generated_at: string;
  executions: ExecutionStatusCountsDto;
  terminal_total: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  approvals: ApprovalOpsSummaryDto;
  schedules: ScheduleOpsSummaryDto;
  mcp_servers: MCPServerOpsSummaryDto;
  mcp_tools: MCPToolOpsSummaryDto;
  recent_executions: ExecutionListItemDto[];
}

export interface DashboardSummaryParams {
  from?: string;
  to?: string;
  recent_limit?: number;
  signal?: AbortSignal;
}

export function getDashboardSummary(params: DashboardSummaryParams = {}) {
  return apiRequest<DashboardSummaryDto>('/ops/dashboard/summary', {
    query: {
      from: params.from,
      to: params.to,
      recent_limit: params.recent_limit ?? 5,
    },
    signal: params.signal,
  });
}
