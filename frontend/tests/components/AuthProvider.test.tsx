import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { AuthProvider } from '../../src/auth/AuthProvider';
import { useAuth } from '../../src/auth/useAuth';
import { clearCsrfCache } from '../../src/api/csrf';
import { mockAuthSession, sessionUnauthorizedBody } from '../fixtures/auth-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function Probe() {
  const { status, user, login, logout } = useAuth();
  return (
    <div>
      <div data-testid="status">{status}</div>
      <div data-testid="user">{user?.username ?? ''}</div>
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(mockAuthSession)));
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    expect(screen.getByTestId('user')).toHaveTextContent('demo');
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
    const fetchMock = vi.fn().mockImplementation((url: string) => {
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
      if (String(url).includes('/auth/session')) {
        return Promise.resolve(jsonResponse(mockAuthSession));
      }
      if (String(url).includes('/auth/csrf')) {
        return Promise.resolve(jsonResponse({ csrf_token: 'csrf-1' }));
      }
      if (String(url).includes('/auth/logout') && init?.method === 'POST') {
        return Promise.resolve(new Response(null, { status: 204 }));
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(mockAuthSession)));
    renderAuthApp();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    notifySessionInvalid();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('unauthenticated'));
    expect(screen.getByTestId('user')).toHaveTextContent('');
  });
});
