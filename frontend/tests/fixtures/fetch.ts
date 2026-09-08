import { vi } from 'vitest';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Wrap a fetch mock so unsafe apiRequest calls can acquire CSRF without
 * each test re-implementing /auth/csrf.
 */
export function stubFetchWithAuth(
  handler: (url: string, init?: RequestInit) => Promise<Response> | Response,
  opts: { csrfToken?: string } = {},
) {
  const csrfToken = opts.csrfToken ?? 'test-csrf-token';
  return vi.fn().mockImplementation((url: string | URL | Request, init?: RequestInit) => {
    const href = String(url);
    if (href.includes('/auth/csrf')) {
      return Promise.resolve(jsonResponse({ csrf_token: csrfToken }));
    }
    return Promise.resolve(handler(href, init));
  });
}
