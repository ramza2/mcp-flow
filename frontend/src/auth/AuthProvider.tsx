import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  getEffectivePermissions,
  getSession,
  login as apiLogin,
  logout as apiLogout,
  type AuthSession,
  type SessionUser,
} from '../api/auth';
import { ApiError, isAbortError, isApiError } from '../api/client';
import { clearCsrfCache } from '../api/csrf';
import { onSessionInvalid } from '../api/sessionEvents';
import { hasPermission as checkPermission, type Permission } from '../domain';

export type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated' | 'error';

export interface AuthState {
  status: AuthStatus;
  session: AuthSession | null;
  user: SessionUser | null;
  error: ApiError | null;
  /** Effective formal permission codes from GET /auth/permissions. */
  permissions: readonly string[];
}

export interface AuthContextValue extends AuthState {
  login: (username: string, password: string) => Promise<AuthSession>;
  logout: () => Promise<void>;
  refreshSession: () => Promise<void>;
  clearSessionLocal: () => void;
  /** UX helper — Backend authorization remains authoritative. */
  hasPermission: (permission: Permission) => boolean;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

const initialState: AuthState = {
  status: 'loading',
  session: null,
  user: null,
  error: null,
  permissions: [],
};

async function loadPermissions(signal?: AbortSignal): Promise<readonly string[]> {
  const body = await getEffectivePermissions({ signal });
  return Array.isArray(body.permission_codes) ? body.permission_codes : [];
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(initialState);
  const bootstrapGen = useRef(0);

  const clearToUnauthenticated = useCallback(() => {
    // Bump generation so in-flight session/permission responses cannot restore auth.
    bootstrapGen.current += 1;
    clearCsrfCache();
    setState({
      status: 'unauthenticated',
      session: null,
      user: null,
      error: null,
      permissions: [],
    });
  }, []);

  const clearSessionLocal = useCallback(() => {
    clearToUnauthenticated();
  }, [clearToUnauthenticated]);

  const applyAuthenticated = useCallback(
    async (session: AuthSession, gen: number, signal?: AbortSignal) => {
      let permissions: readonly string[] = [];
      try {
        permissions = await loadPermissions(signal);
      } catch (error) {
        if (signal?.aborted || isAbortError(error)) return;
        if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
          if (gen !== bootstrapGen.current) return;
          clearToUnauthenticated();
          return;
        }
        // Fail closed for gated UX: keep session, empty permissions.
        permissions = [];
      }
      if (gen !== bootstrapGen.current || signal?.aborted) return;
      setState({
        status: 'authenticated',
        session,
        user: session.user,
        error: null,
        permissions,
      });
    },
    [clearToUnauthenticated],
  );

  const refreshSession = useCallback(async () => {
    const gen = ++bootstrapGen.current;
    const ac = new AbortController();
    setState(prev => ({
      ...prev,
      status: prev.status === 'authenticated' ? prev.status : 'loading',
      error: null,
    }));
    try {
      const session = await getSession({ signal: ac.signal });
      if (gen !== bootstrapGen.current) return;
      await applyAuthenticated(session, gen, ac.signal);
    } catch (error) {
      if (gen !== bootstrapGen.current) return;
      if (isAbortError(error)) return;
      if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
        clearToUnauthenticated();
        return;
      }
      setState({
        status: 'error',
        session: null,
        user: null,
        error: isApiError(error)
          ? error
          : new ApiError({
              status: 0,
              code: 'NETWORK_ERROR',
              message: error instanceof Error ? error.message : 'Session bootstrap failed',
              retryable: true,
            }),
        permissions: [],
      });
    }
  }, [applyAuthenticated, clearToUnauthenticated]);

  useEffect(() => {
    const gen = ++bootstrapGen.current;
    const ac = new AbortController();

    (async () => {
      try {
        const session = await getSession({ signal: ac.signal });
        if (gen !== bootstrapGen.current || ac.signal.aborted) return;
        await applyAuthenticated(session, gen, ac.signal);
      } catch (error) {
        if (gen !== bootstrapGen.current || ac.signal.aborted || isAbortError(error)) return;
        if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
          clearToUnauthenticated();
          return;
        }
        setState({
          status: 'error',
          session: null,
          user: null,
          error: isApiError(error)
            ? error
            : new ApiError({
                status: 0,
                code: 'NETWORK_ERROR',
                message: error instanceof Error ? error.message : 'Session bootstrap failed',
                retryable: true,
              }),
          permissions: [],
        });
      }
    })();

    return () => {
      ac.abort();
    };
  }, [applyAuthenticated, clearToUnauthenticated]);

  useEffect(() => {
    return onSessionInvalid(() => {
      clearToUnauthenticated();
    });
  }, [clearToUnauthenticated]);

  const login = useCallback(
    async (username: string, password: string) => {
      const session = await apiLogin(username, password);
      clearCsrfCache();
      const gen = ++bootstrapGen.current;
      let permissions: readonly string[] = [];
      try {
        permissions = await loadPermissions();
      } catch (error) {
        if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
          if (gen === bootstrapGen.current) {
            clearToUnauthenticated();
          }
          throw error;
        }
        // Fail closed for gated UX: keep session, empty permissions.
        permissions = [];
      }
      if (gen !== bootstrapGen.current) return session;
      setState({
        status: 'authenticated',
        session,
        user: session.user,
        error: null,
        permissions,
      });
      return session;
    },
    [clearToUnauthenticated],
  );

  const logout = useCallback(async () => {
    try {
      await apiLogout();
      clearToUnauthenticated();
    } catch (error) {
      if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
        clearToUnauthenticated();
        return;
      }
      throw error;
    }
  }, [clearToUnauthenticated]);

  const hasPermission = useCallback(
    (permission: Permission) => checkPermission(permission, state.permissions),
    [state.permissions],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      login,
      logout,
      refreshSession,
      clearSessionLocal,
      hasPermission,
    }),
    [state, login, logout, refreshSession, clearSessionLocal, hasPermission],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return ctx;
}
