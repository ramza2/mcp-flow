/**
 * Shared ApiError / abort helpers used by client + CSRF (avoids circular imports).
 */

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
