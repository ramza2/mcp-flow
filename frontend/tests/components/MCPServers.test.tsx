import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPServers from '@/screens/mcp/MCPServers';
import { activeServer, failedCheck, serverList, succeededCheck } from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCPServers — API list', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows loading then server rows from API', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(serverList)),
    );

    renderWithRouter(<MCPServers />, { path: '/mcp/servers', route: '/mcp/servers' });

    await waitFor(() => {
      expect(screen.getByText('Docs MCP')).toBeInTheDocument();
      expect(screen.getByText('Weather MCP')).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'MCP 서버 관리' })).toBeInTheDocument();
      expect(screen.getByText('처음 사용하나요?')).toBeInTheDocument();
      expect(screen.getByRole('combobox', { name: '상태' }).querySelector('option[value="ACTIVE"]')).toHaveTextContent('활성');
    });
  });

  it('shows error state on API failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ error: { code: 'INTERNAL', message: 'boom', request_id: 'req-x' } }, 500),
      ),
    );

    renderWithRouter(<MCPServers />, { path: '/mcp/servers', route: '/mcp/servers' });

    expect(await screen.findByText('boom')).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-x/i)).toBeInTheDocument();
  });

  it('shows empty state when no servers', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
      ),
    );

    renderWithRouter(<MCPServers />, { path: '/mcp/servers', route: '/mcp/servers' });

    expect(await screen.findByText(/등록된 MCP 서버가 없습니다/i)).toBeInTheDocument();
  });

  it('runs per-row connection test and shows SUCCEEDED status', async () => {
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'POST' && url.includes('/connection-tests')) {
        return Promise.resolve(jsonResponse(succeededCheck, 201));
      }
      return Promise.resolve(jsonResponse(serverList));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPServers />, { path: '/mcp/servers', route: '/mcp/servers' });

    await screen.findByText('Docs MCP');
    const testButtons = screen.getAllByTitle('연결 테스트');
    await user.click(testButtons[0]);

    expect(await screen.findByText('연결 성공')).toBeInTheDocument();
  });

  it('shows FAILED status from connection test response', async () => {
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'POST' && url.includes('/connection-tests')) {
        return Promise.resolve(jsonResponse(failedCheck, 201));
      }
      return Promise.resolve(jsonResponse({ items: [activeServer], page: 1, page_size: 20, total: 1, has_next: false }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPServers />, { path: '/mcp/servers', route: '/mcp/servers' });

    await screen.findByText('Docs MCP');
    await user.click(screen.getByTitle('연결 테스트'));

    expect(await screen.findByText('연결 실패')).toBeInTheDocument();
  });
});
