/** Agent registry API helpers — docs/06 §10 / backend agents router. */

import { apiRequest } from './client';
import type {
  AgentCreateRequest,
  AgentDto,
  AgentListDto,
  AgentToolGrantListDto,
  AgentToolGrantPutRequest,
  AgentUpdateRequest,
  AgentVersionCreateRequest,
  AgentVersionDto,
  AgentVersionListDto,
  AgentVersionValidationDto,
  ListParams,
} from './types';

function pageQuery(params: ListParams = {}) {
  return {
    page: params.page ?? 1,
    page_size: params.page_size ?? 20,
    sort: params.sort ?? '-updated_at',
    q: params.q,
    status: params.status,
  };
}

function ifMatchHeaders(lockVersion: number): Record<string, string> {
  return { 'If-Match': String(lockVersion) };
}

export function listAgents(params: ListParams = {}) {
  return apiRequest<AgentListDto>('/agents', {
    query: pageQuery(params),
    signal: params.signal,
  });
}

export function getAgent(agentId: string, signal?: AbortSignal) {
  return apiRequest<AgentDto>(`/agents/${agentId}`, { signal });
}

export function createAgent(body: AgentCreateRequest, signal?: AbortSignal) {
  return apiRequest<AgentDto>('/agents', {
    method: 'POST',
    body,
    signal,
  });
}

export function updateAgent(
  agentId: string,
  body: AgentUpdateRequest,
  lockVersion: number,
  signal?: AbortSignal,
) {
  return apiRequest<AgentDto>(`/agents/${agentId}`, {
    method: 'PATCH',
    body: { ...body, lock_version: lockVersion },
    headers: ifMatchHeaders(lockVersion),
    signal,
  });
}

export function listAgentVersions(
  agentId: string,
  params: Pick<ListParams, 'page' | 'page_size' | 'sort' | 'signal'> = {},
) {
  return apiRequest<AgentVersionListDto>(`/agents/${agentId}/versions`, {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      sort: params.sort ?? '-version_no',
    },
    signal: params.signal,
  });
}

export function getAgentVersion(agentId: string, versionId: string, signal?: AbortSignal) {
  return apiRequest<AgentVersionDto>(`/agents/${agentId}/versions/${versionId}`, { signal });
}

export function createAgentVersion(
  agentId: string,
  body: AgentVersionCreateRequest,
  signal?: AbortSignal,
) {
  return apiRequest<AgentVersionDto>(`/agents/${agentId}/versions`, {
    method: 'POST',
    body,
    signal,
  });
}

export function validateAgentVersion(
  agentId: string,
  versionId: string,
  signal?: AbortSignal,
) {
  return apiRequest<AgentVersionValidationDto>(
    `/agents/${agentId}/versions/${versionId}/validate`,
    { method: 'POST', signal },
  );
}

export function publishAgentVersion(
  agentId: string,
  versionId: string,
  signal?: AbortSignal,
) {
  return apiRequest<AgentVersionDto>(
    `/agents/${agentId}/versions/${versionId}/publish`,
    { method: 'POST', signal },
  );
}

export function deprecateAgentVersion(
  agentId: string,
  versionId: string,
  signal?: AbortSignal,
) {
  return apiRequest<AgentVersionDto>(
    `/agents/${agentId}/versions/${versionId}/deprecate`,
    { method: 'POST', signal },
  );
}

export function listAgentToolGrants(
  agentId: string,
  versionId: string,
  signal?: AbortSignal,
) {
  return apiRequest<AgentToolGrantListDto>(
    `/agents/${agentId}/versions/${versionId}/tool-grants`,
    { signal },
  );
}

export function putAgentToolGrants(
  agentId: string,
  versionId: string,
  body: AgentToolGrantPutRequest,
  signal?: AbortSignal,
) {
  return apiRequest<AgentToolGrantListDto>(
    `/agents/${agentId}/versions/${versionId}/tool-grants`,
    { method: 'PUT', body, signal },
  );
}

export function activateAgent(agentId: string, lockVersion: number, signal?: AbortSignal) {
  return updateAgent(agentId, { status: 'ACTIVE' }, lockVersion, signal);
}
