/** API DTO fixtures for External MCP Discovery tests — aligned with backend schemas. */

import type {
  ExternalMCPCandidateDto,
  ExternalMCPImportDto,
  ExternalMCPReviewDto,
  ExternalMCPSearchDto,
  ExternalMCPSourceDto,
  ExternalMCPSourceListDto,
} from '../../src/api/types';

export const officialSource: ExternalMCPSourceDto = {
  id: 'src-official-001',
  code: 'official-mcp-registry',
  name: 'Official MCP Registry',
  source_type: 'REGISTRY',
  provider_key: 'official.mcp.registry',
  base_url: 'https://registry.modelcontextprotocol.io',
  enabled: true,
  created_at: '2026-10-08T00:00:00Z',
  updated_at: '2026-10-08T00:00:00Z',
};

export const disabledSource: ExternalMCPSourceDto = {
  id: 'src-disabled-001',
  code: 'disabled-registry',
  name: 'Disabled Registry',
  source_type: 'REGISTRY',
  provider_key: 'other.registry',
  base_url: 'https://example.invalid',
  enabled: false,
  created_at: '2026-10-08T00:00:00Z',
  updated_at: '2026-10-08T00:00:00Z',
};

export const sourceList: ExternalMCPSourceListDto = {
  items: [officialSource, disabledSource],
};

export const remoteCandidate: ExternalMCPCandidateDto = {
  id: 'cand-remote-001',
  search_id: 'search-001',
  source_id: officialSource.id,
  external_key: 'io.example/slack-mcp',
  name: 'Slack MCP',
  description: 'Slack integration over Streamable HTTP',
  version: '1.2.0',
  license: 'MIT',
  repository_url: 'https://github.com/example/slack-mcp',
  homepage_url: 'https://example.com/slack-mcp',
  transport_type: 'STREAMABLE_HTTP',
  endpoint_url: 'https://mcp.example.com/slack',
  review_state: 'UNREVIEWED',
  imported_mcp_server_id: null,
  discovered_at: '2026-10-08T01:00:00Z',
};

export const packageOnlyCandidate: ExternalMCPCandidateDto = {
  id: 'cand-pkg-001',
  search_id: 'search-001',
  source_id: officialSource.id,
  external_key: 'io.example/local-only',
  name: 'Local Package MCP',
  description: 'Package/local-only candidate without remote endpoint',
  version: '0.1.0',
  license: 'Apache-2.0',
  repository_url: 'javascript:alert(1)',
  homepage_url: 'data:text/html,hi',
  transport_type: null,
  endpoint_url: null,
  review_state: 'UNREVIEWED',
  imported_mcp_server_id: null,
  discovered_at: '2026-10-08T01:00:01Z',
};

export function searchSucceeded(
  candidates: ExternalMCPCandidateDto[] = [remoteCandidate, packageOnlyCandidate],
): ExternalMCPSearchDto {
  return {
    id: 'search-001',
    source_id: officialSource.id,
    query: 'slack',
    status: 'SUCCEEDED',
    requested_limit: 20,
    candidate_count: candidates.length,
    error_code: null,
    error_message: null,
    requested_by: 'user-001',
    started_at: '2026-10-08T01:00:00Z',
    finished_at: '2026-10-08T01:00:02Z',
    candidates,
  };
}

export const searchFailed: ExternalMCPSearchDto = {
  id: 'search-failed-001',
  source_id: officialSource.id,
  query: 'boom',
  status: 'FAILED',
  requested_limit: 20,
  candidate_count: 0,
  error_code: 'EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE',
  error_message: 'Official Registry temporarily unavailable',
  requested_by: 'user-001',
  started_at: '2026-10-08T01:00:00Z',
  finished_at: '2026-10-08T01:00:01Z',
  candidates: [],
};

export function reviewResponse(
  candidateId: string,
  decision: 'APPROVE' | 'REJECT',
  reviewState: 'APPROVED' | 'REJECTED',
): ExternalMCPReviewDto {
  return {
    id: `rev-${candidateId}-${decision.toLowerCase()}`,
    candidate_id: candidateId,
    decision,
    comment: null,
    reviewed_by: 'user-001',
    reviewed_at: '2026-10-08T01:05:00Z',
    review_state: reviewState,
  };
}

export function importResponse(
  candidateId: string,
  opts: { created?: boolean; mcpServerId?: string } = {},
): ExternalMCPImportDto {
  return {
    candidate_id: candidateId,
    mcp_server_id: opts.mcpServerId ?? 'srv-draft-imported-001',
    created: opts.created ?? true,
    server_status: 'DRAFT',
    transport_type: 'STREAMABLE_HTTP',
  };
}
