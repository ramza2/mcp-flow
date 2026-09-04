/**
 * Shared fetch client for /api/v1 (same-origin Traefik routing).
 * No Axios / React Query — native fetch only.
 */

import type { ApiErrorBody } from './types';

export const API_V1_PREFIX = '/api/v1';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown[];
  readonly requestId: string | null;
  readonly retryable: boolean;

  constructor(opts: {
    status: number;
    code: string;
    message: string;
    details?: unknown[];
    requestId?: string | null;
    retryable?: boolean;
  }) {
    super(opts.message);
    this.name = 'ApiError';
    this.status = opts.status;
    this.code = opts.code;
    this.details = opts.details ?? [];
    this.requestId = opts.requestId ?? null;
    this.retryable = opts.retryable ?? false;
  }
}

export function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true;
  if (error instanceof Error && error.name === 'AbortError') return true;
  return false;
}

export function isApiError(error: unknown): error is ApiError {
  if (error instanceof ApiError) return true;
  if (typeof error !== 'object' || error === null) return false;
  const e = error as Partial<ApiError>;
  return typeof e.status === 'number' && typeof e.code === 'string' && typeof e.message === 'string';
}

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
        message: text.slice(0, 200) || `HTTP ${response.status}`,
        requestId: requestIdHeader,
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

export interface RequestOptions {
  method?: string;
  query?: Record<string, string | number | boolean | undefined | null>;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, signal, headers } = options;
  const url = buildUrl(path, query);

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      signal,
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
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

  if (response.status === 204) {
    return undefined as T;
  }

  if (!response.ok) {
    throw await parseError(response);
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
