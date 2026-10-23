/** Real AgentRun analysis + Tool selection API. No fake executions or MCP calls. */
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
