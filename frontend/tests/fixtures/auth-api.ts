import type { AuthSession } from '../../src/api/authTypes';

export const mockAuthSession: AuthSession = {
  session_id: '11111111-1111-4111-8111-111111111111',
  user: {
    id: '22222222-2222-4222-8222-222222222222',
    username: 'demo',
    display_name: 'Demo User',
    email: 'demo@example.com',
    status: 'ACTIVE',
  },
  issued_at: '2026-09-08T00:00:00.000Z',
  expires_at: '2026-09-08T08:00:00.000Z',
};

export function sessionUnauthorizedBody() {
  return {
    error: {
      code: 'AUTH_SESSION_INVALID',
      message: 'Session is invalid or expired',
      details: [],
      retryable: false,
    },
  };
}

export function invalidCredentialsBody() {
  return {
    error: {
      code: 'AUTH_INVALID_CREDENTIALS',
      message: 'Invalid username or password',
      details: [],
      retryable: false,
    },
  };
}

export function csrfInvalidBody() {
  return {
    error: {
      code: 'AUTH_CSRF_INVALID',
      message: 'CSRF token is invalid',
      details: [],
      retryable: false,
    },
  };
}
