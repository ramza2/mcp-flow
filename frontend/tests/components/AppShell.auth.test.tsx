import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { AuthProvider } from '../../src/auth/AuthProvider';
import AppShell from '../../src/components/layout/AppShell';
import { clearCsrfCache } from '../../src/api/csrf';
import { mockAuthSession, mockEffectivePermissions } from '../fixtures/auth-api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function authFetchMock(extra?: (url: string, init?: RequestInit) => Response | null) {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const href = String(url);
    const fromExtra = extra?.(href, init);
    if (fromExtra) return Promise.resolve(fromExtra);
    if (href.includes('/auth/session')) {
      return Promise.resolve(jsonResponse(mockAuthSession));
    }
    if (href.includes('/auth/permissions')) {
      return Promise.resolve(jsonResponse(mockEffectivePermissions));
    }
    if (href.includes('/auth/csrf')) {
      return Promise.resolve(jsonResponse({ csrf_token: 'csrf-1' }));
    }
    if (href.includes('/auth/logout') && init?.method === 'POST') {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    return Promise.resolve(jsonResponse({}));
  });
}

describe('AppShell session user', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearCsrfCache();
  });

  it('shows real session user and calls logout API', async () => {
    const fetchMock = authFetchMock();
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

  it('uses Korean navigation while keeping the existing schedule route and permissions', async () => {
    vi.stubGlobal('fetch', authFetchMock());
    const router = createMemoryRouter(
      [{
        path: '/', element: <AppShell />,
        children: [
          { index: true, element: <div>홈 화면</div> },
          { path: 'schedules', element: <div>예약 목록 화면</div> },
        ],
      }],
      { initialEntries: ['/'] },
    );
    render(<AuthProvider><RouterProvider router={router} /></AuthProvider>);

    await waitFor(() => expect(screen.getByRole('link', { name: '대시보드' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '업무 실행' }));
    const schedulesLink = screen.getByRole('link', { name: '예약 실행' });
    expect(schedulesLink).toHaveAttribute('href', '/schedules');
    fireEvent.click(schedulesLink);
    expect(await screen.findByText('예약 목록 화면')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/schedules');

    fireEvent.click(screen.getByRole('button', { name: '메뉴 접기' }));
    expect(screen.getByRole('button', { name: '메뉴 펼치기' })).toBeInTheDocument();
    expect(screen.getByTitle('업무 실행')).toBeInTheDocument();
  });

  it('hides gated nav children without permission and keeps parent when any child remains', async () => {
    vi.stubGlobal(
      'fetch',
      authFetchMock((href) => {
        if (href.includes('/auth/permissions')) {
          return jsonResponse({
            permission_codes: ['execution.read', 'agent.read'],
          });
        }
        return null;
      }),
    );
    const router = createMemoryRouter(
      [{
        path: '/', element: <AppShell />,
        children: [{ index: true, element: <div>홈</div> }],
      }],
      { initialEntries: ['/'] },
    );
    render(<AuthProvider><RouterProvider router={router} /></AuthProvider>);

    await waitFor(() => expect(screen.getByRole('link', { name: '대시보드' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '업무 실행' }));
    expect(screen.getByRole('link', { name: '실행 이력' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '에이전트 실행' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '예약 실행' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '승인 요청' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '구성 관리' }));
    expect(screen.getByRole('link', { name: '에이전트 관리' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '워크플로 관리' })).toBeInTheDocument();

    expect(screen.queryByRole('button', { name: 'MCP' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '시스템 관리' })).toBeInTheDocument();
  });
});
