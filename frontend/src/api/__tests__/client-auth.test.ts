import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiRequest, ApiError, API_V1_PREFIX } from '../client';
import { clearCsrfCache } from '../csrf';
import { onSessionInvalid } from '../sessionEvents';
import { csrfInvalidBody, sessionUnauthorizedBody } from '../../../tests/fixtures/auth-api';

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

describe('apiRequest auth/csrf', () => {
  beforeEach(() => {
    clearCsrfCache();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearCsrfCache();
  });

  it('sends credentials: same-origin on GET without CSRF header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    await apiRequest('/mcp/servers');
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_V1_PREFIX}/mcp/servers`,
      expect.objectContaining({ method: 'GET', credentials: 'same-origin' }),
    );
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBeUndefined();
  });

  it('does not attach CSRF on GET', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    await apiRequest('/mcp/servers');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBeUndefined();
  });

  it('fetches CSRF once and attaches header on POST', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('/auth/csrf')) {
        return Promise.resolve(jsonResponse({ csrf_token: 'csrf-1' }));
      }
      return Promise.resolve(jsonResponse({ ok: true }));
    });
    vi.stubGlobal('fetch', fetchMock);

    await apiRequest('/mcp/servers', { method: 'POST', body: { name: 'x' } });

    const csrfCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes('/auth/csrf'));
    expect(csrfCalls).toHaveLength(1);
    const postCall = fetchMock.mock.calls.find(c => String(c[0]).includes('/mcp/servers'));
    expect(postCall?.[1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-1' }),
      }),
    );
  });

  it('shares a single CSRF fetch across concurrent POSTs', async () => {
    let csrfResolvers = 0;
    let resolveCsrf!: (value: Response) => void;
    const csrfPromise = new Promise<Response>(resolve => {
      resolveCsrf = resolve;
    });

    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('/auth/csrf')) {
        csrfResolvers += 1;
        return csrfPromise;
      }
      return Promise.resolve(jsonResponse({ ok: true }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const p1 = apiRequest('/mcp/servers', { method: 'POST', body: { a: 1 } });
    const p2 = apiRequest('/mcp/tools', { method: 'POST', body: { b: 2 } });
    await Promise.resolve();
    expect(csrfResolvers).toBe(1);
    resolveCsrf(jsonResponse({ csrf_token: 'shared' }));
    await Promise.all([p1, p2]);
    expect(csrfResolvers).toBe(1);
  });

  it('retries once after AUTH_CSRF_INVALID then succeeds', async () => {
    let csrfCount = 0;
    let postCount = 0;
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).includes('/auth/csrf')) {
        csrfCount += 1;
        return Promise.resolve(jsonResponse({ csrf_token: `csrf-${csrfCount}` }));
      }
      if (init?.method === 'POST') {
        postCount += 1;
        if (postCount === 1) {
          return Promise.resolve(jsonResponse(csrfInvalidBody(), 403));
        }
        return Promise.resolve(jsonResponse({ ok: true }));
      }
      return Promise.resolve(jsonResponse({ ok: true }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await apiRequest<{ ok: boolean }>('/mcp/servers', { method: 'POST', body: {} });
    expect(result).toEqual({ ok: true });
    expect(csrfCount).toBe(2);
    expect(postCount).toBe(2);
  });

  it('throws after a second AUTH_CSRF_INVALID', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('/auth/csrf')) {
        return Promise.resolve(jsonResponse({ csrf_token: 'csrf-x' }));
      }
      return Promise.resolve(jsonResponse(csrfInvalidBody(), 403));
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(apiRequest('/mcp/servers', { method: 'POST', body: {} })).rejects.toMatchObject({
      status: 403,
      code: 'AUTH_CSRF_INVALID',
    });
  });

  it('omits CSRF for login', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ session_id: 's' }));
    vi.stubGlobal('fetch', fetchMock);
    await apiRequest('/auth/login', {
      method: 'POST',
      body: { username: 'u', password: 'p' },
      csrf: 'omit',
      suppressSessionInvalidation: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/auth/login');
  });

  it('notifies session invalidation on AUTH_SESSION_INVALID', async () => {
    const spy = vi.fn();
    const unsubscribe = onSessionInvalid(spy);
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sessionUnauthorizedBody(), 401));
    vi.stubGlobal('fetch', fetchMock);

    await expect(apiRequest('/mcp/servers')).rejects.toBeInstanceOf(ApiError);
    expect(spy).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it('does not notify when suppressSessionInvalidation is set', async () => {
    const spy = vi.fn();
    const unsubscribe = onSessionInvalid(spy);
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sessionUnauthorizedBody(), 401));
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      apiRequest('/auth/session', { suppressSessionInvalidation: true }),
    ).rejects.toMatchObject({ code: 'AUTH_SESSION_INVALID' });
    expect(spy).not.toHaveBeenCalled();
    unsubscribe();
  });

  it('does not retry when aborted after CSRF invalid', async () => {
    const ac = new AbortController();
    let postCount = 0;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('/auth/csrf')) {
        return Promise.resolve(jsonResponse({ csrf_token: 'csrf-1' }));
      }
      postCount += 1;
      ac.abort();
      return Promise.resolve(jsonResponse(csrfInvalidBody(), 403));
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      apiRequest('/mcp/servers', { method: 'POST', body: {}, signal: ac.signal }),
    ).rejects.toSatisfy(isAbortErrorLike);
    expect(postCount).toBe(1);
  });
});

function isAbortErrorLike(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
