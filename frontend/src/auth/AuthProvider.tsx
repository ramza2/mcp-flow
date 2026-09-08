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
import { getSession, login as apiLogin, logout as apiLogout, type AuthSession, type SessionUser } from '../api/auth';
import { ApiError, isAbortError, isApiError } from '../api/client';
import { clearCsrfCache } from '../api/csrf';
import { onSessionInvalid } from '../api/sessionEvents';

export type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated' | 'error';

export interface AuthState {
  status: AuthStatus;
  session: AuthSession | null;
  user: SessionUser | null;
  error: ApiError | null;
}

export interface AuthContextValue extends AuthState {
  login: (username: string, password: string) => Promise<AuthSession>;
  logout: () => Promise<void>;
  refreshSession: () => Promise<void>;
  clearSessionLocal: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const initialState: AuthState = {
  status: 'loading',
  session: null,
  user: null,
  error: null,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(initialState);
  const bootstrapGen = useRef(0);

  const clearSessionLocal = useCallback(() => {
    clearCsrfCache();
    setState({
      status: 'unauthenticated',
      session: null,
      user: null,
      error: null,
    });
  }, []);

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
      setState({
        status: 'authenticated',
        session,
        user: session.user,
        error: null,
      });
    } catch (error) {
      if (gen !== bootstrapGen.current) return;
      if (isAbortError(error)) return;
      if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
        clearCsrfCache();
        setState({
          status: 'unauthenticated',
          session: null,
          user: null,
          error: null,
        });
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
      });
    }
  }, []);

  useEffect(() => {
    const gen = ++bootstrapGen.current;
    const ac = new AbortController();

    (async () => {
      try {
        const session = await getSession({ signal: ac.signal });
        if (gen !== bootstrapGen.current || ac.signal.aborted) return;
        setState({
          status: 'authenticated',
          session,
          user: session.user,
          error: null,
        });
      } catch (error) {
        if (gen !== bootstrapGen.current || ac.signal.aborted || isAbortError(error)) return;
        if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
          clearCsrfCache();
          setState({
            status: 'unauthenticated',
            session: null,
            user: null,
            error: null,
          });
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
        });
      }
    })();

    return () => {
      ac.abort();
    };
  }, []);

  useEffect(() => {
    return onSessionInvalid(() => {
      clearCsrfCache();
      setState({
        status: 'unauthenticated',
        session: null,
        user: null,
        error: null,
      });
    });
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const session = await apiLogin(username, password);
    clearCsrfCache();
    setState({
      status: 'authenticated',
      session,
      user: session.user,
      error: null,
    });
    return session;
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
      clearCsrfCache();
      setState({
        status: 'unauthenticated',
        session: null,
        user: null,
        error: null,
      });
    } catch (error) {
      if (isApiError(error) && error.status === 401 && error.code === 'AUTH_SESSION_INVALID') {
        clearCsrfCache();
        setState({
          status: 'unauthenticated',
          session: null,
          user: null,
          error: null,
        });
        return;
      }
      throw error;
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      login,
      logout,
      refreshSession,
      clearSessionLocal,
    }),
    [state, login, logout, refreshSession, clearSessionLocal],
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
