import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  activateMCPTool,
  createToolVerification,
  deactivateMCPTool,
  getToolVerification,
  listToolVerifications,
  putMCPToolPolicy,
  updateMCPTool,
} from '../mcp';
import { discoveredTool, pendingVerification, toolPolicy, verificationList } from '../../../tests/fixtures/mcp-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('mcp tool lifecycle API helpers', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('updateMCPTool sends PATCH with If-Match', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(discoveredTool));
    vi.stubGlobal('fetch', fetchMock);

    await updateMCPTool(
      discoveredTool.id,
      { display_name: 'Renamed', tags: ['a'] },
      3,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/mcp/tools/${discoveredTool.id}`,
      expect.objectContaining({
        method: 'PATCH',
        headers: expect.objectContaining({
          'If-Match': '3',
          'Content-Type': 'application/json',
        }),
      }),
    );
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      display_name: 'Renamed',
      tags: ['a'],
    });
  });

  it('activateMCPTool / deactivateMCPTool send If-Match without body', async () => {
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/activate')) {
        return Promise.resolve(jsonResponse({ ...discoveredTool, status: 'ACTIVE' }));
      }
      if (url.includes('/deactivate')) {
        return Promise.resolve(jsonResponse({ ...discoveredTool, status: 'INACTIVE' }));
      }
      return Promise.resolve(new Response('not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    await activateMCPTool(discoveredTool.id, 2);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/mcp/tools/${discoveredTool.id}/activate`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'If-Match': '2' }),
        body: undefined,
      }),
    );

    await deactivateMCPTool(discoveredTool.id, 4);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/mcp/tools/${discoveredTool.id}/deactivate`,
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'If-Match': '4' }),
      }),
    );
  });

  it('putMCPToolPolicy omits If-Match on create and includes it on update', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(toolPolicy)));
    vi.stubGlobal('fetch', fetchMock);

    const body = {
      risk_class: 'READ_ONLY' as const,
      timeout_ms: 1000,
      max_attempts: 1,
      max_result_bytes: 100,
    };

    await putMCPToolPolicy(discoveredTool.id, body);
    const createHeaders = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(createHeaders['If-Match']).toBeUndefined();

    await putMCPToolPolicy(discoveredTool.id, body, { lockVersion: 7 });
    expect(fetchMock.mock.calls[1][1]).toEqual(
      expect.objectContaining({
        method: 'PUT',
        headers: expect.objectContaining({ 'If-Match': '7' }),
      }),
    );
  });

  it('verification list/create/detail use version-scoped paths', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(verificationList))
      .mockResolvedValueOnce(jsonResponse(pendingVerification))
      .mockResolvedValueOnce(jsonResponse(pendingVerification));
    vi.stubGlobal('fetch', fetchMock);

    await listToolVerifications(discoveredTool.id, 'ver-valid-001', { page: 1, page_size: 20 });
    expect(fetchMock.mock.calls[0][0]).toContain(
      `/api/v1/mcp/tools/${discoveredTool.id}/versions/ver-valid-001/verifications?page=1&page_size=20`,
    );

    await createToolVerification(discoveredTool.id, 'ver-valid-001', {
      status: 'PENDING',
      criteria_version: 'tool-verification-v1',
    });
    const createInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect(fetchMock.mock.calls[1][0]).toBe(
      `/api/v1/mcp/tools/${discoveredTool.id}/versions/ver-valid-001/verifications`,
    );
    expect(JSON.parse(String(createInit.body))).toEqual({
      status: 'PENDING',
      criteria_version: 'tool-verification-v1',
    });

    await getToolVerification(discoveredTool.id, 'ver-valid-001', pendingVerification.id);
    expect(fetchMock.mock.calls[2][0]).toBe(
      `/api/v1/mcp/tools/${discoveredTool.id}/versions/ver-valid-001/verifications/${pendingVerification.id}`,
    );
  });
});
