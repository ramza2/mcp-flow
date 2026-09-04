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
  MCPToolVersionDto,
  MCPToolVersionListDto,
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
