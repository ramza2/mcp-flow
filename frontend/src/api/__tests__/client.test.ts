import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest, ApiError, isAbortError, isApiError, API_V1_PREFIX } from '../client';

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

describe('apiRequest', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns parsed JSON on success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ ok: true })));
    const result = await apiRequest<{ ok: boolean }>('/mcp/servers');
    expect(result).toEqual({ ok: true });
    expect(fetch).toHaveBeenCalledWith(
      `${API_V1_PREFIX}/mcp/servers`,
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('throws ApiError with canonical error body and request_id', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: 'VALIDATION_ERROR',
              message: 'Invalid input',
              details: [{ field: 'name' }],
              request_id: 'req-abc',
              retryable: false,
            },
          },
          422,
        ),
      ),
    );
    await expect(apiRequest('/mcp/servers', { method: 'POST', body: {} })).rejects.toMatchObject({
      status: 422,
      code: 'VALIDATION_ERROR',
      message: 'Invalid input',
      requestId: 'req-abc',
      retryable: false,
    });
  });

  it('wraps network failures as ApiError NETWORK_ERROR', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(apiRequest('/mcp/servers')).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
      status: 0,
      retryable: true,
    });
  });

  it('re-throws AbortError without wrapping', async () => {
    const abortErr = new DOMException('Aborted', 'AbortError');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abortErr));
    await expect(apiRequest('/mcp/servers', { signal: new AbortController().signal })).rejects.toBe(abortErr);
    expect(isAbortError(abortErr)).toBe(true);
  });

  it('handles non-JSON error responses with generic message and preserves X-Request-ID', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() =>
        Promise.resolve(
          new Response('<html>Internal Server Error stacktrace</html>', {
            status: 502,
            headers: { 'X-Request-ID': 'req-non-json-1' },
          }),
        ),
      ),
    );
    try {
      await apiRequest('/mcp/servers');
      expect.fail('should throw');
    } catch (e) {
      expect(isApiError(e)).toBe(true);
      expect(e).toMatchObject({
        code: 'NON_JSON_ERROR',
        status: 502,
        message: 'Server returned a non-JSON error response (HTTP 502).',
        requestId: 'req-non-json-1',
        retryable: true,
      });
      expect((e as ApiError).message).not.toContain('stacktrace');
      expect((e as ApiError).message).not.toContain('<html>');
    }
  });

  it('serializes query params and omits empty values', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ items: [] })));
    await apiRequest('/mcp/tools', {
      query: { page: 2, q: 'search', status: '', mcp_server_id: 'srv-1' },
    });
    expect(fetch).toHaveBeenCalledWith(
      `${API_V1_PREFIX}/mcp/tools?page=2&q=search&mcp_server_id=srv-1`,
      expect.any(Object),
    );
  });

  it('returns undefined for 204 No Content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    const result = await apiRequest('/mcp/servers/srv-1', { method: 'DELETE' });
    expect(result).toBeUndefined();
  });

  it('isApiError detects ApiError by duck typing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ error: { code: 'X', message: 'bad' } }, 400),
      ),
    );
    try {
      await apiRequest('/x');
    } catch (e) {
      expect(isApiError(e)).toBe(true);
      expect((e as ApiError).status).toBe(400);
    }
  });

  it('ApiError exposes details array', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ error: { code: 'X', message: 'bad', details: ['a'] } }, 400),
      ),
    );
    try {
      await apiRequest('/x');
      expect.fail('should throw');
    } catch (e) {
      expect(isApiError(e)).toBe(true);
      expect((e as ApiError).details).toEqual(['a']);
    }
  });
});
