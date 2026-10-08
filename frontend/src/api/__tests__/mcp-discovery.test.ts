import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createExternalMCPSearch,
  getExternalMCPCandidate,
  getExternalMCPSearch,
  importExternalMCPCandidate,
  listExternalMCPSources,
  reviewExternalMCPCandidate,
} from '../mcpDiscovery';
import {
  importResponse,
  officialSource,
  remoteCandidate,
  reviewResponse,
  searchSucceeded,
  sourceList,
} from '../../../tests/fixtures/mcp-discovery-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('mcpDiscovery API helpers', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('listExternalMCPSources GETs /mcp-discovery/sources', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sourceList));
    vi.stubGlobal('fetch', fetchMock);

    const res = await listExternalMCPSources();
    expect(res.items[0].code).toBe(officialSource.code);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/mcp-discovery/sources',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('createExternalMCPSearch POSTs source_id/q/limit', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(searchSucceeded()));
    vi.stubGlobal('fetch', fetchMock);

    await createExternalMCPSearch({
      source_id: officialSource.id,
      q: 'slack',
      limit: 20,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/mcp-discovery/searches',
      expect.objectContaining({ method: 'POST' }),
    );
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      source_id: officialSource.id,
      q: 'slack',
      limit: 20,
    });
  });

  it('getExternalMCPSearch / getExternalMCPCandidate use path ids', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(searchSucceeded()))
      .mockResolvedValueOnce(jsonResponse(remoteCandidate));
    vi.stubGlobal('fetch', fetchMock);

    await getExternalMCPSearch('search-001');
    await getExternalMCPCandidate(remoteCandidate.id);

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/mcp-discovery/searches/search-001');
    expect(fetchMock.mock.calls[1][0]).toBe(
      `/api/v1/mcp-discovery/candidates/${remoteCandidate.id}`,
    );
  });

  it('reviewExternalMCPCandidate POSTs decision', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(reviewResponse(remoteCandidate.id, 'APPROVE', 'APPROVED')));
    vi.stubGlobal('fetch', fetchMock);

    await reviewExternalMCPCandidate(remoteCandidate.id, { decision: 'APPROVE' });

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/mcp-discovery/candidates/${remoteCandidate.id}/reviews`,
      expect.objectContaining({ method: 'POST' }),
    );
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({ decision: 'APPROVE' });
  });

  it('importExternalMCPCandidate POSTs import', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(importResponse(remoteCandidate.id)));
    vi.stubGlobal('fetch', fetchMock);

    const res = await importExternalMCPCandidate(remoteCandidate.id);
    expect(res.server_status).toBe('DRAFT');
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/mcp-discovery/candidates/${remoteCandidate.id}/import`,
      expect.objectContaining({ method: 'POST' }),
    );
  });
});
