/** MCP registry API helpers — docs/06 §8–9 / backend mcp_servers + mcp_tools routers. */

import { apiRequest } from './client';
import type {
  ConnectionTestDto,
  DiscoveryCreateRequest,
  DiscoveryDto,
  DiscoveryListDto,
  ListParams,
  MCPServerCreateRequest,
  MCPServerDto,
  MCPServerListDto,
  MCPToolDto,
  MCPToolListDto,
  MCPToolPolicyDto,
  MCPToolPolicyPutRequest,
  MCPToolUpdateRequest,
  MCPToolVersionDto,
  MCPToolVersionListDto,
  ToolVerificationCreateRequest,
  ToolVerificationDto,
  ToolVerificationListDto,
} from './types';

function pageQuery(params: ListParams = {}) {
  return {
    page: params.page ?? 1,
    page_size: params.page_size ?? 20,
    sort: params.sort ?? '-updated_at',
    q: params.q,
    status: params.status,
    transport_type: params.transport_type,
    mcp_server_id: params.mcp_server_id,
  };
}

function ifMatchHeaders(lockVersion: number): Record<string, string> {
  return { 'If-Match': String(lockVersion) };
}

export function listMCPServers(params: ListParams = {}) {
  return apiRequest<MCPServerListDto>('/mcp/servers', {
    query: pageQuery(params),
    signal: params.signal,
  });
}

export function getMCPServer(serverId: string, signal?: AbortSignal) {
  return apiRequest<MCPServerDto>(`/mcp/servers/${serverId}`, { signal });
}

export function createMCPServer(body: MCPServerCreateRequest, signal?: AbortSignal) {
  return apiRequest<MCPServerDto>('/mcp/servers', {
    method: 'POST',
    body,
    signal,
  });
}

export function activateMCPServer(serverId: string, signal?: AbortSignal) {
  return apiRequest<MCPServerDto>(`/mcp/servers/${serverId}/activate`, {
    method: 'POST',
    signal,
  });
}

export function deactivateMCPServer(serverId: string, signal?: AbortSignal) {
  return apiRequest<MCPServerDto>(`/mcp/servers/${serverId}/deactivate`, {
    method: 'POST',
    signal,
  });
}

export function connectionTestMCPServer(serverId: string, signal?: AbortSignal) {
  return apiRequest<ConnectionTestDto>(`/mcp/servers/${serverId}/connection-tests`, {
    method: 'POST',
    signal,
  });
}

export function createDiscovery(
  serverId: string,
  body: DiscoveryCreateRequest = {},
  signal?: AbortSignal,
) {
  return apiRequest<DiscoveryDto>(`/mcp/servers/${serverId}/discoveries`, {
    method: 'POST',
    body: {
      mode: body.mode ?? 'FULL',
      apply_changes: body.apply_changes ?? false,
    },
    signal,
  });
}

export function listDiscoveries(
  serverId: string,
  params: Pick<ListParams, 'page' | 'page_size' | 'signal'> = {},
) {
  return apiRequest<DiscoveryListDto>(`/mcp/servers/${serverId}/discoveries`, {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
    },
    signal: params.signal,
  });
}

export function listServerTools(
  serverId: string,
  params: ListParams = {},
) {
  return apiRequest<MCPToolListDto>(`/mcp/servers/${serverId}/tools`, {
    query: pageQuery(params),
    signal: params.signal,
  });
}

export function listMCPTools(params: ListParams = {}) {
  return apiRequest<MCPToolListDto>('/mcp/tools', {
    query: pageQuery(params),
    signal: params.signal,
  });
}

export function getMCPTool(toolId: string, signal?: AbortSignal) {
  return apiRequest<MCPToolDto>(`/mcp/tools/${toolId}`, { signal });
}

export function listToolVersions(
  toolId: string,
  params: Pick<ListParams, 'page' | 'page_size' | 'signal'> = {},
) {
  return apiRequest<MCPToolVersionListDto>(`/mcp/tools/${toolId}/versions`, {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
    },
    signal: params.signal,
  });
}

export function getToolVersion(toolId: string, versionId: string, signal?: AbortSignal) {
  return apiRequest<MCPToolVersionDto>(`/mcp/tools/${toolId}/versions/${versionId}`, {
    signal,
  });
}

export function updateMCPTool(
  toolId: string,
  body: MCPToolUpdateRequest,
  lockVersion: number,
  signal?: AbortSignal,
) {
  return apiRequest<MCPToolDto>(`/mcp/tools/${toolId}`, {
    method: 'PATCH',
    body,
    headers: ifMatchHeaders(lockVersion),
    signal,
  });
}

export function activateMCPTool(toolId: string, lockVersion: number, signal?: AbortSignal) {
  return apiRequest<MCPToolDto>(`/mcp/tools/${toolId}/activate`, {
    method: 'POST',
    headers: ifMatchHeaders(lockVersion),
    signal,
  });
}

export function deactivateMCPTool(toolId: string, lockVersion: number, signal?: AbortSignal) {
  return apiRequest<MCPToolDto>(`/mcp/tools/${toolId}/deactivate`, {
    method: 'POST',
    headers: ifMatchHeaders(lockVersion),
    signal,
  });
}

export function getMCPToolPolicy(toolId: string, signal?: AbortSignal) {
  return apiRequest<MCPToolPolicyDto>(`/mcp/tools/${toolId}/policy`, { signal });
}

export function putMCPToolPolicy(
  toolId: string,
  body: MCPToolPolicyPutRequest,
  options: { lockVersion?: number; signal?: AbortSignal } = {},
) {
  return apiRequest<MCPToolPolicyDto>(`/mcp/tools/${toolId}/policy`, {
    method: 'PUT',
    body,
    headers:
      options.lockVersion !== undefined ? ifMatchHeaders(options.lockVersion) : undefined,
    signal: options.signal,
  });
}

export function listToolVerifications(
  toolId: string,
  versionId: string,
  params: Pick<ListParams, 'page' | 'page_size' | 'signal'> = {},
) {
  return apiRequest<ToolVerificationListDto>(
    `/mcp/tools/${toolId}/versions/${versionId}/verifications`,
    {
      query: {
        page: params.page ?? 1,
        page_size: params.page_size ?? 20,
      },
      signal: params.signal,
    },
  );
}

export function createToolVerification(
  toolId: string,
  versionId: string,
  body: ToolVerificationCreateRequest,
  signal?: AbortSignal,
) {
  return apiRequest<ToolVerificationDto>(
    `/mcp/tools/${toolId}/versions/${versionId}/verifications`,
    {
      method: 'POST',
      body,
      signal,
    },
  );
}

export function getToolVerification(
  toolId: string,
  versionId: string,
  verificationId: string,
  signal?: AbortSignal,
) {
  return apiRequest<ToolVerificationDto>(
    `/mcp/tools/${toolId}/versions/${versionId}/verifications/${verificationId}`,
    { signal },
  );
}
