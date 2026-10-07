/**
 * Execution history + cancel API — docs/06 §14 / PR #58.
 * Safe projections only; no plan/input snapshots or ToolCall meta.
 */

import type {
  ExecutionSourceType,
  ExecutionStatus,
  ExecutionTriggerType,
  StepStatus,
} from '../domain/types';
import { apiRequest } from './client';
import type { JsonValue } from './types';

export interface ExecutionSourceDto {
  type: ExecutionSourceType;
  version_id: string | null;
  logical_id: string | null;
  name: string | null;
}

export interface ExecutionListItemDto {
  id: string;
  source_type: ExecutionSourceType;
  trigger_type: ExecutionTriggerType;
  requester_id: string;
  agent_request_id: string | null;
  agent_version_id: string | null;
  workflow_version_id: string | null;
  schedule_occurrence_id: string | null;
  parent_execution_id: string | null;
  status: ExecutionStatus;
  error_code: string | null;
  error_category: string | null;
  trace_id: string | null;
  requested_at: string;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested_at: string | null;
  step_count: number;
  completed_step_count: number;
  failed_step_count: number;
  duration_ms: number | null;
  source: ExecutionSourceDto | null;
}

export interface ExecutionListDto {
  items: ExecutionListItemDto[];
  page: number;
  page_size: number;
  total: number;
}

export interface PlanLimitsSummaryDto {
  max_steps: number | null;
  max_duration_seconds: number | null;
  max_parallelism: number | null;
  max_loop_iterations: number | null;
}

export interface ExecutionDetailDto extends ExecutionListItemDto {
  plan_schema_version: string;
  plan_hash: string;
  plan_limits: PlanLimitsSummaryDto | null;
  result_summary: Record<string, JsonValue> | null;
  retention_until: string | null;
}

export interface ToolCallSafeDto {
  id: string;
  mcp_server_id: string;
  mcp_tool_version_id: string;
  protocol_era: string;
  protocol_version: string;
  transport_type: string;
  normalized_status: string;
  request_bytes: number | null;
  response_bytes: number | null;
  started_at: string;
  first_byte_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  time_to_first_byte_ms: number | null;
}

export interface StepAttemptSafeDto {
  id: string;
  attempt_no: number;
  status: string;
  error_layer: string | null;
  error_code: string | null;
  error_category: string | null;
  is_retryable: boolean | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  tool_calls: ToolCallSafeDto[];
}

export interface ExecutionStepListItemDto {
  id: string;
  execution_id: string;
  step_key: string;
  step_type: string;
  parent_step_id: string | null;
  sequence_hint: number;
  mcp_tool_version_id: string | null;
  iteration_no: number | null;
  status: StepStatus | string;
  attempt_count: number;
  condition_result: boolean | null;
  ready_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_category: string | null;
  duration_ms: number | null;
}

export interface ExecutionStepListDto {
  items: ExecutionStepListItemDto[];
}

export interface ExecutionStepDetailDto extends ExecutionStepListItemDto {
  attempts: StepAttemptSafeDto[];
}

export interface ExecutionCancelResultDto {
  id: string;
  status: ExecutionStatus;
  cancel_requested_at: string | null;
  finished_at: string | null;
}

export type ExecutionSort =
  | 'requested_at'
  | '-requested_at'
  | 'started_at'
  | '-started_at'
  | 'finished_at'
  | '-finished_at'
  | 'status'
  | '-status';

export interface ExecutionListParams {
  page?: number;
  page_size?: number;
  status?: string;
  source_type?: string;
  trigger_type?: string;
  requester_id?: string;
  agent_version_id?: string;
  workflow_version_id?: string;
  schedule_occurrence_id?: string;
  parent_execution_id?: string;
  tool_version_id?: string;
  error_code?: string;
  from?: string;
  to?: string;
  q?: string;
  sort?: ExecutionSort;
  signal?: AbortSignal;
}

export function listExecutions(params: ExecutionListParams = {}) {
  return apiRequest<ExecutionListDto>('/executions', {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      status: params.status,
      source_type: params.source_type,
      trigger_type: params.trigger_type,
      requester_id: params.requester_id,
      agent_version_id: params.agent_version_id,
      workflow_version_id: params.workflow_version_id,
      schedule_occurrence_id: params.schedule_occurrence_id,
      parent_execution_id: params.parent_execution_id,
      tool_version_id: params.tool_version_id,
      error_code: params.error_code,
      from: params.from,
      to: params.to,
      q: params.q,
      sort: params.sort ?? '-requested_at',
    },
    signal: params.signal,
  });
}

export function getExecution(executionId: string, signal?: AbortSignal) {
  return apiRequest<ExecutionDetailDto>(`/executions/${executionId}`, { signal });
}

export function listExecutionSteps(executionId: string, signal?: AbortSignal) {
  return apiRequest<ExecutionStepListDto>(`/executions/${executionId}/steps`, {
    signal,
  });
}

export function getExecutionStep(
  executionId: string,
  stepExecutionId: string,
  signal?: AbortSignal,
) {
  return apiRequest<ExecutionStepDetailDto>(
    `/executions/${executionId}/steps/${stepExecutionId}`,
    { signal },
  );
}

export function cancelExecution(
  executionId: string,
  reason?: string | null,
  signal?: AbortSignal,
) {
  return apiRequest<ExecutionCancelResultDto>(`/executions/${executionId}/cancel`, {
    method: 'POST',
    body: reason != null && reason !== '' ? { reason } : {},
    signal,
  });
}
