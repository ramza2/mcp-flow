/**
 * Memory-only CSRF token cache with single-flight acquisition.
 * Never persist to localStorage / sessionStorage / IndexedDB.
 *
 * Uses a dedicated fetch (not apiRequest) to avoid circular imports with the CSRF-aware client.
 */

import { API_V1_PREFIX, ApiError, isAbortError } from './clientCore';
import type { CsrfTokenResponse } from './authTypes';

let cachedToken: string | null = null;
let inflight: Promise<string> | null = null;

export function getCachedCsrfToken(): string | null {
  return cachedToken;
}

export function clearCsrfCache(): void {
  cachedToken = null;
  inflight = null;
}

/** Test-only: seed memory cache so unsafe requests skip /auth/csrf fetch. */
export function setCachedCsrfTokenForTests(token: string | null): void {
  cachedToken = token;
  inflight = null;
}

async function fetchCsrfToken(signal?: AbortSignal): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${API_V1_PREFIX}/auth/csrf`, {
      method: 'GET',
      credentials: 'same-origin',
      signal,
      headers: { Accept: 'application/json' },
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError({
      status: 0,
      code: 'NETWORK_ERROR',
      message: error instanceof Error ? error.message : 'Network request failed',
      retryable: true,
    });
  }

  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = `Request failed with status ${response.status}`;
    let requestId = response.headers.get('X-Request-ID');
    let retryable = response.status >= 500;
    try {
      const body = (await response.json()) as { error?: { code?: string; message?: string; request_id?: string; retryable?: boolean } };
      code = body.error?.code ?? code;
      message = body.error?.message ?? message;
      requestId = body.error?.request_id ?? requestId;
      retryable = Boolean(body.error?.retryable);
    } catch {
      // keep defaults
    }
    throw new ApiError({ status: response.status, code, message, requestId, retryable });
  }

  const data = (await response.json()) as CsrfTokenResponse;
  if (!data.csrf_token) {
    throw new ApiError({
      status: response.status,
      code: 'INVALID_CSRF_RESPONSE',
      message: 'CSRF response missing csrf_token',
    });
  }
  return data.csrf_token;
}

export async function ensureCsrfToken(signal?: AbortSignal): Promise<string> {
  if (signal?.aborted) {
    throw new DOMException('Aborted', 'AbortError');
  }
  if (cachedToken) {
    return cachedToken;
  }
  if (inflight) {
    return inflight;
  }

  inflight = (async () => {
    try {
      const token = await fetchCsrfToken(signal);
      if (signal?.aborted) {
        throw new DOMException('Aborted', 'AbortError');
      }
      cachedToken = token;
      return token;
    } finally {
      inflight = null;
    }
  })();

  return inflight;
}
