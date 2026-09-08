import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { AuthProvider } from '../../src/auth/AuthProvider';
import AppShell from '../../src/components/layout/AppShell';
import { clearCsrfCache } from '../../src/api/csrf';
import { mockAuthSession } from '../fixtures/auth-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('AppShell session user', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearCsrfCache();
  });

  it('shows real session user and calls logout API', async () => {
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

    const router = createMemoryRouter(
      [
        { path: '/login', element: <div>Login Screen</div> },
        {
          path: '/',
          element: <AppShell />,
          children: [{ index: true, element: <div>Dashboard Body</div> }],
        },
      ],
      { initialEntries: ['/'] },
    );

    render(
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getAllByText('Demo User').length).toBeGreaterThan(0));
    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
    expect(screen.queryByText('Super Admin')).not.toBeInTheDocument();
    expect(screen.queryByText('admin@mcpflow.io')).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByText('Demo User')[0]);
    expect(screen.getByText('demo@example.com')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /로그아웃/i }));

    await waitFor(() => expect(screen.getByText('Login Screen')).toBeInTheDocument());
    const logoutCalls = fetchMock.mock.calls.filter(
      c => String(c[0]).includes('/auth/logout') && (c[1] as RequestInit | undefined)?.method === 'POST',
    );
    expect(logoutCalls).toHaveLength(1);
    expect(logoutCalls[0][1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        headers: expect.objectContaining({ 'X-CSRF-Token': expect.any(String) }),
      }),
    );
  });
});
