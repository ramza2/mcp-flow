/**
 * MRTR Input API — docs/06 §15.
 * Backend translates accepted responses into MCP inputResponses + requestState.
 * Frontend must never send or receive requestState.
 */

import { apiRequest } from './client';

export interface MrtrInputRequestDto {
  id: string;
  status: string;
  source: string;
  execution_id: string;
  step_execution_id: string;
  round_no: number;
  input_requests: Record<string, unknown>;
  expires_at: string;
  requested_at: string;
  answered_at: string | null;
}

export interface MrtrInputRequestListDto {
  items: MrtrInputRequestDto[];
}

export interface MrtrResponseCreateDto {
  input_request_id: string;
  execution_id: string;
  status: string;
  resume_enqueued: boolean;
  execution_status: string;
  step_status: string;
}

export interface MrtrRejectDto {
  input_request_id: string;
  execution_id: string;
  status: string;
  execution_status: string;
  step_status: string;
}

export function listInputRequests(
  executionId: string,
  params: { status?: string; signal?: AbortSignal } = {},
) {
  return apiRequest<MrtrInputRequestListDto>(
    `/executions/${executionId}/input-requests`,
    {
      query: { status: params.status },
      signal: params.signal,
    },
  );
}

export function getInputRequest(
  executionId: string,
  inputRequestId: string,
  signal?: AbortSignal,
) {
  return apiRequest<MrtrInputRequestDto>(
    `/executions/${executionId}/input-requests/${inputRequestId}`,
    { signal },
  );
}

export function submitInputResponse(
  executionId: string,
  inputRequestId: string,
  responses: Record<string, unknown>,
  signal?: AbortSignal,
) {
  return apiRequest<MrtrResponseCreateDto>(
    `/executions/${executionId}/input-requests/${inputRequestId}/responses`,
    {
      method: 'POST',
      body: { responses },
      signal,
    },
  );
}

export function rejectInputRequest(
  executionId: string,
  inputRequestId: string,
  signal?: AbortSignal,
) {
  return apiRequest<MrtrRejectDto>(
    `/executions/${executionId}/input-requests/${inputRequestId}/reject`,
    {
      method: 'POST',
      signal,
    },
  );
}
