/** External MCP Discovery API helpers — docs/06 §19 / backend external_discovery router. */

import { apiRequest } from './client';
import type {
  ExternalMCPCandidateDto,
  ExternalMCPImportDto,
  ExternalMCPReviewCreateRequest,
  ExternalMCPReviewDto,
  ExternalMCPSearchCreateRequest,
  ExternalMCPSearchDto,
  ExternalMCPSourceListDto,
} from './types';

export function listExternalMCPSources(signal?: AbortSignal) {
  return apiRequest<ExternalMCPSourceListDto>('/mcp-discovery/sources', { signal });
}

export function createExternalMCPSearch(
  body: ExternalMCPSearchCreateRequest,
  signal?: AbortSignal,
) {
  return apiRequest<ExternalMCPSearchDto>('/mcp-discovery/searches', {
    method: 'POST',
    body: {
      source_id: body.source_id,
      q: body.q,
      limit: body.limit ?? 20,
    },
    signal,
  });
}

export function getExternalMCPSearch(searchId: string, signal?: AbortSignal) {
  return apiRequest<ExternalMCPSearchDto>(`/mcp-discovery/searches/${searchId}`, {
    signal,
  });
}

export function getExternalMCPCandidate(candidateId: string, signal?: AbortSignal) {
  return apiRequest<ExternalMCPCandidateDto>(
    `/mcp-discovery/candidates/${candidateId}`,
    { signal },
  );
}

export function reviewExternalMCPCandidate(
  candidateId: string,
  body: ExternalMCPReviewCreateRequest,
  signal?: AbortSignal,
) {
  return apiRequest<ExternalMCPReviewDto>(
    `/mcp-discovery/candidates/${candidateId}/reviews`,
    {
      method: 'POST',
      body: {
        decision: body.decision,
        ...(body.comment !== undefined ? { comment: body.comment } : {}),
      },
      signal,
    },
  );
}

export function importExternalMCPCandidate(candidateId: string, signal?: AbortSignal) {
  return apiRequest<ExternalMCPImportDto>(
    `/mcp-discovery/candidates/${candidateId}/import`,
    {
      method: 'POST',
      signal,
    },
  );
}
