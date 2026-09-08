/**
 * Shared fetch client for /api/v1 (same-origin Traefik routing).
 * No Axios / React Query — native fetch only.
 *
 * Cookie Session: credentials: 'same-origin'
 * CSRF: auto-attach X-CSRF-Token on unsafe methods (except csrf: 'omit')
 */

import type { ApiErrorBody } from './types';
import {
  API_V1_PREFIX,
  ApiError,
  isAbortError,
  isApiError,
} from './clientCore';
import { clearCsrfCache, ensureCsrfToken } from './csrf';
import { notifySessionInvalid } from './sessionEvents';

export { API_V1_PREFIX, ApiError, isAbortError, isApiError };

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

function buildUrl(path: string, query?: Record<string, string | number | boolean | undefined | null>): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  const url = new URL(`${API_V1_PREFIX}${normalized}`, window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.pathname + url.search;
}

async function parseError(response: Response): Promise<ApiError> {
  const requestIdHeader = response.headers.get('X-Request-ID');
  let body: ApiErrorBody | null = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text) as ApiErrorBody;
    } catch {
      return new ApiError({
        status: response.status,
        code: 'NON_JSON_ERROR',
        message: `Server returned a non-JSON error response (HTTP ${response.status}).`,
        requestId: requestIdHeader,
        retryable: response.status >= 500,
      });
    }
  }
  const err = body?.error;
  return new ApiError({
    status: response.status,
    code: err?.code ?? `HTTP_${response.status}`,
    message: err?.message ?? `Request failed with status ${response.status}`,
    details: Array.isArray(err?.details) ? err.details : [],
    requestId: err?.request_id ?? requestIdHeader,
    retryable: Boolean(err?.retryable),
  });
}

export type CsrfMode = 'auto' | 'omit';

export interface RequestOptions {
  method?: string;
  query?: Record<string, string | number | boolean | undefined | null>;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** CSRF attachment policy. Login uses 'omit'. Default 'auto'. */
  csrf?: CsrfMode;
  /**
   * When true, AUTH_SESSION_INVALID does not fire the global invalidation callback.
   * Used for login failure and session bootstrap 401.
   */
  suppressSessionInvalidation?: boolean;
  /** Internal: skip CSRF retry (already retried once). */
  _csrfRetried?: boolean;
}

async function parseSuccessBody<T>(response: Response): Promise<T> {
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  if (!text) {
    return undefined as T;
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError({
      status: response.status,
      code: 'NON_JSON_RESPONSE',
      message: 'Server returned non-JSON response',
      requestId: response.headers.get('X-Request-ID'),
    });
  }
}

async function executeFetch(
  url: string,
  method: string,
  headers: Record<string, string>,
  body: unknown | undefined,
  signal?: AbortSignal,
): Promise<Response> {
  try {
    return await fetch(url, {
      method,
      signal,
      credentials: 'same-origin',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
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
}

function handleSessionInvalidation(error: ApiError, options: RequestOptions): void {
  if (options.suppressSessionInvalidation) return;
  if (error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
    clearCsrfCache();
    notifySessionInvalid();
  }
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase();
  const { query, body, signal, headers: extraHeaders } = options;
  const csrfMode = options.csrf ?? 'auto';
  const url = buildUrl(path, query);

  if (signal?.aborted) {
    throw new DOMException('Aborted', 'AbortError');
  }

  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    ...extraHeaders,
  };

  if (csrfMode === 'auto' && UNSAFE_METHODS.has(method) && !headers['X-CSRF-Token']) {
    const token = await ensureCsrfToken(signal);
    if (signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError');
    }
    headers['X-CSRF-Token'] = token;
  }

  const response = await executeFetch(url, method, headers, body, signal);

  if (response.ok || response.status === 204) {
    return parseSuccessBody<T>(response);
  }

  const error = await parseError(response);

  if (
    error.status === 403 &&
    error.code === 'AUTH_CSRF_INVALID' &&
    csrfMode === 'auto' &&
    UNSAFE_METHODS.has(method) &&
    !options._csrfRetried
  ) {
    if (signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError');
    }
    clearCsrfCache();
    return apiRequest<T>(path, {
      ...options,
      _csrfRetried: true,
      headers: { ...extraHeaders },
    });
  }

  handleSessionInvalidation(error, options);
  throw error;
}
