import { vi } from 'vitest';
import type { AuthContextValue } from '../../src/auth/AuthProvider';
import { hasPermission as checkPermission, type Permission } from '../../src/domain';
import { mockAuthSession } from './auth-api';

/** Controllable AuthContext for unit tests (PermissionGate / gated screens). */
export function buildAuthContext(
  overrides: Partial<AuthContextValue> = {},
): AuthContextValue {
  const permissions = overrides.permissions ?? [];
  const hasPermission =
    overrides.hasPermission ??
    ((permission: Permission) => checkPermission(permission, permissions));

  return {
    status: 'authenticated',
    session: mockAuthSession,
    user: mockAuthSession.user,
    error: null,
    login: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    refreshSession: vi.fn().mockResolvedValue(undefined),
    clearSessionLocal: vi.fn(),
    ...overrides,
    permissions,
    hasPermission,
  };
}
