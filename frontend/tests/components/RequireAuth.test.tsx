import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { AuthProvider } from '../../src/auth/AuthProvider';
import RequireAuth from '../../src/components/RequireAuth';
import { clearCsrfCache } from '../../src/api/csrf';
import { mockAuthSession, sessionUnauthorizedBody } from '../fixtures/auth-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderProtected(fetchImpl: typeof fetch) {
  vi.stubGlobal('fetch', fetchImpl);
  const router = createMemoryRouter(
    [
      { path: '/login', element: <div>Login Screen</div> },
      {
        path: '/',
        element: <RequireAuth />,
        children: [{ path: 'mcp/tools', element: <div>Protected Tools</div> }],
      },
    ],
    { initialEntries: ['/mcp/tools'] },
  );
  return render(
    <AuthProvider>
      <RouterProvider router={router} />
    </AuthProvider>,
  );
}

describe('RequireAuth', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearCsrfCache();
  });

  it('does not show protected content while loading', () => {
    let resolveSession!: (value: Response) => void;
    const pending = new Promise<Response>(resolve => {
      resolveSession = resolve;
    });
    renderProtected(vi.fn().mockReturnValue(pending));
    expect(screen.getByText(/세션을 확인하는 중/i)).toBeInTheDocument();
    expect(screen.queryByText('Protected Tools')).not.toBeInTheDocument();
    resolveSession(jsonResponse(mockAuthSession));
  });

  it('redirects unauthenticated users to login', async () => {
    renderProtected(vi.fn().mockResolvedValue(jsonResponse(sessionUnauthorizedBody(), 401)));
    expect(await screen.findByText('Login Screen')).toBeInTheDocument();
    expect(screen.queryByText('Protected Tools')).not.toBeInTheDocument();
  });

  it('renders outlet when authenticated', async () => {
    renderProtected(vi.fn().mockResolvedValue(jsonResponse(mockAuthSession)));
    expect(await screen.findByText('Protected Tools')).toBeInTheDocument();
  });

  it('shows retry UI on bootstrap error', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    renderProtected(fetchMock);
    expect(await screen.findByText(/세션을 확인할 수 없습니다/i)).toBeInTheDocument();
    fetchMock.mockResolvedValueOnce(jsonResponse(mockAuthSession));
    fireEvent.click(screen.getByRole('button', { name: /다시 시도/i }));
    await waitFor(() => expect(screen.getByText('Protected Tools')).toBeInTheDocument());
  });
});
