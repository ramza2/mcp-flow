/** Real AgentRun planning + explicit Execution creation API. */
import { apiRequest } from './client';
import type { JsonValue } from './types';
import type { AgentRequestStatus } from '../domain';

export interface RunnableAgentDto {
  id: string;
  name: string;
  description: string | null;
  agent_version_id: string;
  version_no: number;
}

export interface AgentRunRequestDto {
  id: string;
  conversation_id: string;
  agent_version_id: string;
  status: AgentRequestStatus;
  raw_request_text: string;
  structured_request: Record<string, JsonValue> | null;
  rejection_code: string | null;
  created_at: string;
  analyzed_at: string | null;
}

export function listRunnableAgents(signal?: AbortSignal) {
  return apiRequest<{ items: RunnableAgentDto[] }>('/agent-requests/available-agents', { signal });
}

export function submitAgentRun(agentId: string, requestText: string) {
  return apiRequest<AgentRunRequestDto>('/agent-requests', {
    method: 'POST',
    body: { agent_id: agentId, request_text: requestText },
  });
}

export function analyzeAgentRun(requestId: string) {
  return apiRequest<AgentRunRequestDto>(`/agent-requests/${requestId}/analyze`, {
    method: 'POST',
  });
}

export interface AgentRunSelectionDto {
  id: string;
  decision: 'AUTO_SELECT' | 'CONFIRM' | 'CLARIFY' | 'NO_MATCH';
  selected_tool_version_id: string | null;
  selected_tool_name: string | null;
  confidence: number | null;
  reason_summary: string | null;
  created_at: string;
}

export interface AgentRunSelectionResultDto {
  request: AgentRunRequestDto;
  selection: AgentRunSelectionDto | null;
}

export function getAgentRunSelection(requestId: string, signal?: AbortSignal) {
  return apiRequest<AgentRunSelectionResultDto>(
    `/agent-requests/${requestId}/selection`, { signal },
  );
}

export function selectToolForAgentRun(requestId: string) {
  return apiRequest<AgentRunSelectionResultDto>(
    `/agent-requests/${requestId}/select-tool`,
    { method: 'POST' },
  );
}

export function getAgentRunRequest(requestId: string, signal?: AbortSignal) {
  return apiRequest<AgentRunRequestDto>(`/agent-requests/${requestId}`, { signal });
}

export function getConversationLatestAgentRun(conversationId: string, signal?: AbortSignal) {
  return apiRequest<AgentRunRequestDto>(
    `/agent-requests/conversations/${conversationId}/latest`,
    { signal },
  );
}


export interface AgentRunParameterDto {
  id: string;
  tool_version_id: string;
  is_complete: boolean;
  missing_fields: string[];
  created_at: string;
}

export interface AgentRunPlanDto {
  id: string;
  plan_hash: string;
  goal: string | null;
  step_names: string[];
  step_count: number;
  created_at: string;
}

export interface AgentRunValidationDto {
  id: string;
  decision: string;
  confirmation_required: boolean;
  error_count: number;
  warning_count: number;
  created_at: string;
}

export interface AgentRunQuestionFieldDto {
  name: string;
  value_type: string;
  description: string | null;
  required: boolean;
}

export interface AgentRunClarificationDto {
  id: string;
  request_type: 'MISSING_PARAMETER' | 'TOOL_CONFIRMATION' | 'PLAN_CONFIRMATION';
  prompt_text: string;
  fields: AgentRunQuestionFieldDto[];
  requested_at: string;
}

export interface AgentRunProgressDto {
  request: AgentRunRequestDto;
  parameter_build: AgentRunParameterDto | null;
  plan: AgentRunPlanDto | null;
  validation: AgentRunValidationDto | null;
  open_clarification: AgentRunClarificationDto | null;
}

export function getAgentRunProgress(requestId: string, signal?: AbortSignal) {
  return apiRequest<AgentRunProgressDto>(`/agent-requests/${requestId}/progress`, { signal });
}

export function buildAgentRunParameters(requestId: string) {
  return apiRequest<AgentRunProgressDto>(`/agent-requests/${requestId}/build-parameters`, { method: 'POST' });
}

export function generateAgentRunPlan(requestId: string) {
  return apiRequest<AgentRunProgressDto>(`/agent-requests/${requestId}/generate-plan`, { method: 'POST' });
}

export function validateAgentRunPlan(requestId: string) {
  return apiRequest<AgentRunProgressDto>(`/agent-requests/${requestId}/validate-plan`, { method: 'POST' });
}

export function respondToAgentRunClarification(
  requestId: string,
  clarificationId: string,
  responsePayload: Record<string, JsonValue>,
) {
  return apiRequest<{
    agent_request_id: string;
    clarification_id: string;
    clarification_status: string;
    agent_request_status: AgentRequestStatus;
  }>(`/agent-requests/${requestId}/clarifications/${clarificationId}/responses`, {
    method: 'POST',
    body: { response_payload: responsePayload },
  });
}

/** Execution is created only by the user's explicit action; queue/worker runs asynchronously. */
export interface AgentRunExecutionCreateDto {
  id: string;
  status: string;
  source_type: string;
  trigger_type: string;
  agent_request_id: string;
  agent_version_id: string;
  plan_hash: string;
  requested_at: string;
  step_count: number;
}

export function createAgentRunExecution(requestId: string) {
  // Stable per-request key prevents duplicate creation on timeout/F5/manual retry.
  return apiRequest<AgentRunExecutionCreateDto>(`/agent-requests/${requestId}/executions`, {
    method: 'POST',
    headers: { 'Idempotency-Key': `agent-run:${requestId}` },
  });
}
