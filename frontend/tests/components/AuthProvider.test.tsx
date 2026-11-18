import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { AuthProvider } from '../../src/auth/AuthProvider';
import { useAuth } from '../../src/auth/useAuth';
import { clearCsrfCache } from '../../src/api/csrf';
import { mockAuthSession, mockEffectivePermissions, sessionUnauthorizedBody } from '../fixtures/auth-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function authFetch(url: string, init?: RequestInit): Promise<Response> | null {
  const href = String(url);
  if (href.includes('/auth/permissions')) {
    return Promise.resolve(jsonResponse(mockEffectivePermissions));
  }
  if (href.includes('/auth/csrf')) {
    return Promise.resolve(jsonResponse({ csrf_token: 'csrf-1' }));
  }
  if (href.includes('/auth/logout') && init?.method === 'POST') {
    return Promise.resolve(new Response(null, { status: 204 }));
  }
  return null;
}

function Probe() {
  const { status, user, login, logout, permissions, hasPermission } = useAuth();
  return (
    <div>
      <div data-testid="status">{status}</div>
      <div data-testid="user">{user?.username ?? ''}</div>
      <div data-testid="perm-count">{permissions.length}</div>
      <div data-testid="has-exec-read">{hasPermission('execution.read') ? 'yes' : 'no'}</div>
      <button type="button" onClick={() => void login('demo', 'secret-password')}>
        do-login
      </button>
      <button type="button" onClick={() => void logout()}>
        do-logout
      </button>
    </div>
  );
}

function renderAuthApp() {
  const router = createMemoryRouter([{ path: '/', element: <Probe /> }], { initialEntries: ['/'] });
  return render(
    <AuthProvider>
      <RouterProvider router={router} />
    </AuthProvider>,
  );
}

describe('AuthProvider', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearCsrfCache();
  });

  it('bootstraps authenticated from GET /auth/session 200', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        const handled = authFetch(url, init);
        if (handled) return handled;
        if (String(url).includes('/auth/session')) {
          return Promise.resolve(jsonResponse(mockAuthSession));
        }
        return Promise.resolve(jsonResponse({}));
      }),
    );
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    expect(screen.getByTestId('user')).toHaveTextContent('demo');
    expect(screen.getByTestId('perm-count')).toHaveTextContent(
      String(mockEffectivePermissions.permission_codes.length),
    );
    expect(screen.getByTestId('has-exec-read')).toHaveTextContent('yes');
  });

  it('bootstraps unauthenticated from GET /auth/session 401', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(sessionUnauthorizedBody(), 401)));
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated'));
  });

  it('bootstraps error on network failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('error'));
  });

  it('login success stores session', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const handled = authFetch(url, init);
      if (handled) return handled;
      if (String(url).includes('/auth/session')) {
        return Promise.resolve(jsonResponse(sessionUnauthorizedBody(), 401));
      }
      if (String(url).includes('/auth/login')) {
        return Promise.resolve(jsonResponse(mockAuthSession));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated'));
    screen.getByText('do-login').click();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    expect(screen.getByTestId('user')).toHaveTextContent('demo');
  });

  it('logout success clears state', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const handled = authFetch(url, init);
      if (handled) return handled;
      if (String(url).includes('/auth/session')) {
        return Promise.resolve(jsonResponse(mockAuthSession));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    screen.getByText('do-logout').click();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated'));
    expect(screen.getByTestId('user')).toHaveTextContent('');
  });

  it('clears to unauthenticated on session-invalid event', async () => {
    const { notifySessionInvalid } = await import('../../src/api/sessionEvents');
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        const handled = authFetch(url, init);
        if (handled) return handled;
        if (String(url).includes('/auth/session')) {
          return Promise.resolve(jsonResponse(mockAuthSession));
        }
        return Promise.resolve(jsonResponse({}));
      }),
    );
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    notifySessionInvalid();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated'));
    expect(screen.getByTestId('user')).toHaveTextContent('');
    expect(screen.getByTestId('perm-count')).toHaveTextContent('0');
  });
});
