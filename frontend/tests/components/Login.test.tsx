import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { AuthProvider } from '../../src/auth/AuthProvider';
import Login from '../../src/screens/Login';
import { clearCsrfCache } from '../../src/api/csrf';
import { invalidCredentialsBody, mockAuthSession, sessionUnauthorizedBody } from '../fixtures/auth-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderLogin(initialPath = '/login') {
  const router = createMemoryRouter(
    [
      { path: '/login', element: <Login /> },
      { path: '/mcp/tools', element: <div>Tools Page</div> },
      { path: '/', element: <div>Home Page</div> },
    ],
    {
      initialEntries: [
        {
          pathname: '/login',
          state: initialPath === '/login' ? undefined : { from: { pathname: initialPath } },
        },
      ],
    },
  );
  return {
    router,
    ...render(
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>,
    ),
  };
}

describe('Login screen', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearCsrfCache();
  });

  it('shows local validation for empty username/password', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(sessionUnauthorizedBody(), 401)));
    renderLogin();
    await waitFor(() => expect(screen.getByRole('button', { name: /sign in/i })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
    expect(await screen.findByText('아이디와 비밀번호를 입력하세요.')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1); // bootstrap only
  });

  it('calls real login API and shows generic error on 401', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('/auth/session')) {
        return Promise.resolve(jsonResponse(sessionUnauthorizedBody(), 401));
      }
      if (String(url).includes('/auth/login')) {
        return Promise.resolve(jsonResponse(invalidCredentialsBody(), 401));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderLogin();
    await waitFor(() => expect(screen.getByLabelText(/사용자 아이디/i)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/사용자 아이디/i), { target: { value: 'demo' } });
    fireEvent.change(screen.getByLabelText(/비밀번호/i), { target: { value: 'wrong-password' } });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
    expect(await screen.findByText('아이디 또는 비밀번호를 확인하세요.')).toBeInTheDocument();
    expect(screen.queryByText(/LOCKED|INACTIVE|password_hash|사용자 없음/i)).not.toBeInTheDocument();
  });

  it('navigates to intended route on success and prevents double submit', async () => {
    let loginCalls = 0;
    let resolveLogin!: (value: Response) => void;
    const loginPromise = new Promise<Response>(resolve => {
      resolveLogin = resolve;
    });
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('/auth/session')) {
        return Promise.resolve(jsonResponse(sessionUnauthorizedBody(), 401));
      }
      if (String(url).includes('/auth/login')) {
        loginCalls += 1;
        return loginPromise;
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { router } = renderLogin('/mcp/tools');
    await waitFor(() => expect(screen.getByLabelText(/사용자 아이디/i)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/사용자 아이디/i), { target: { value: 'demo' } });
    fireEvent.change(screen.getByLabelText(/비밀번호/i), { target: { value: 'correct-horse' } });
    const submit = screen.getByRole('button', { name: /sign in/i });
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(loginCalls).toBe(1));
    resolveLogin(jsonResponse(mockAuthSession));
    await waitFor(() => expect(router.state.location.pathname).toBe('/mcp/tools'));
    expect(screen.getByText('Tools Page')).toBeInTheDocument();
  });
});
