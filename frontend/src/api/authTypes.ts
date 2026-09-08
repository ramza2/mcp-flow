/**
 * Auth / Session API DTOs — aligned with backend/app/schemas/auth_session.py (docs/06 §3).
 */

/** Canonical User status (docs/05). */
export const USER_STATUSES = ['ACTIVE', 'INACTIVE', 'LOCKED'] as const;
export type UserStatus = (typeof USER_STATUSES)[number];

export interface SessionUser {
  id: string;
  username: string;
  display_name: string;
  email: string;
  status: UserStatus;
}

export interface AuthSession {
  session_id: string;
  user: SessionUser;
  issued_at: string;
  expires_at: string;
}

export interface CsrfTokenResponse {
  csrf_token: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}
