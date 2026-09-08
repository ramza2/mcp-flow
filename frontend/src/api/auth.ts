/**
 * Auth API wrappers — Cookie Session + CSRF (docs/06 §3).
 * Session token is HttpOnly; never read from document.cookie.
 */

import { apiRequest, type RequestOptions } from './client';
import type { AuthSession, CsrfTokenResponse, LoginRequest } from './authTypes';

export type { AuthSession, CsrfTokenResponse, LoginRequest, SessionUser, UserStatus } from './authTypes';

type AuthCallOptions = Pick<RequestOptions, 'signal'>;

export function login(username: string, password: string, options: AuthCallOptions = {}): Promise<AuthSession> {
  const body: LoginRequest = { username, password };
  return apiRequest<AuthSession>('/auth/login', {
    method: 'POST',
    body,
    signal: options.signal,
    csrf: 'omit',
    suppressSessionInvalidation: true,
  });
}

export function getSession(options: AuthCallOptions = {}): Promise<AuthSession> {
  return apiRequest<AuthSession>('/auth/session', {
    method: 'GET',
    signal: options.signal,
    suppressSessionInvalidation: true,
  });
}

export function getCsrf(options: AuthCallOptions = {}): Promise<CsrfTokenResponse> {
  return apiRequest<CsrfTokenResponse>('/auth/csrf', {
    method: 'GET',
    signal: options.signal,
    // Session-invalid during CSRF fetch should still notify (caller decides).
    // Bootstrap paths that need silence use getSession first.
  });
}

export function logout(options: AuthCallOptions = {}): Promise<void> {
  return apiRequest<void>('/auth/logout', {
    method: 'POST',
    signal: options.signal,
  });
}
