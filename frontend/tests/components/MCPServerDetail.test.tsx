import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPServerDetail from '@/screens/mcp/MCPServerDetail';
import {
  activeServer,
  appliedDiscovery,
  discoveryList,
  previewDiscovery,
  succeededCheck,
  toolList,
} from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCPServerDetail — API detail', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubDetailFetch() {
    return vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes(`/mcp/servers/${activeServer.id}`) && !url.includes('/tools') && !url.includes('/discoveries') && !url.includes('/connection-tests') && !url.includes('/activate') && !url.includes('/deactivate')) {
        return Promise.resolve(jsonResponse(activeServer));
      }
      if (url.includes('/tools')) {
        return Promise.resolve(jsonResponse(toolList));
      }
      if (url.includes('/discoveries') && init?.method !== 'POST') {
        return Promise.resolve(jsonResponse(discoveryList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
  }

  it('loads server overview from API', async () => {
    vi.stubGlobal('fetch', stubDetailFetch());

    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${activeServer.id}`,
    });

    expect(await screen.findByText('Docs MCP')).toBeInTheDocument();
    expect(screen.getAllByText('Streamable HTTP').length).toBeGreaterThan(0);
    expect(screen.getByText('2026-07-28')).toBeInTheDocument();
  });

  it('shows 404 error state for missing server', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ error: { code: 'NOT_FOUND', message: 'not found' } }, 404),
      ),
    );

    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: '/mcp/servers/missing-id',
    });

    expect(await screen.findByText(/서버를 찾을 수 없습니다/i)).toBeInTheDocument();
  });

  it('activates DRAFT/INACTIVE server', async () => {
    const draft = { ...activeServer, status: 'DRAFT' as const };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'POST' && url.includes('/activate')) {
        return Promise.resolve(jsonResponse({ ...draft, status: 'ACTIVE' }));
      }
      if (url.includes('/tools')) return Promise.resolve(jsonResponse(toolList));
      if (url.includes('/discoveries')) return Promise.resolve(jsonResponse(discoveryList));
      if (url.includes('/mcp/servers/')) return Promise.resolve(jsonResponse(draft));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${activeServer.id}`,
    });

    await screen.findByText('Docs MCP');
    await user.click(screen.getByRole('button', { name: /^Activate$/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/activate'),
        expect.objectContaining({ method: 'POST' }),
      );
    });
  });

  it('shows Activate RESOURCE_CONFLICT without unhandled rejection', async () => {
    const draft = { ...activeServer, status: 'DRAFT' as const };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'POST' && url.includes('/activate')) {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'RESOURCE_CONFLICT',
                message: 'Server cannot be activated from current status',
                request_id: 'req-conflict-1',
                retryable: false,
              },
            },
            409,
          ),
        );
      }
      if (url.includes('/tools')) return Promise.resolve(jsonResponse(toolList));
      if (url.includes('/discoveries')) return Promise.resolve(jsonResponse(discoveryList));
      if (url.includes('/mcp/servers/')) return Promise.resolve(jsonResponse(draft));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${activeServer.id}`,
    });

    await screen.findByText('Docs MCP');
    await user.click(screen.getByRole('button', { name: /^Activate$/i }));

    expect(await screen.findByText(/Server cannot be activated from current status/i)).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-conflict-1/i)).toBeInTheDocument();
    expect(screen.getByText('DRAFT')).toBeInTheDocument();
  });

  it('keeps Overview when Tools secondary API fails', async () => {
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/tools')) {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'INTERNAL_ERROR',
                message: 'tools boom',
                request_id: 'req-tools-500',
              },
            },
            500,
          ),
        );
      }
      if (url.includes('/discoveries')) return Promise.resolve(jsonResponse(discoveryList));
      if (url.includes(`/mcp/servers/${activeServer.id}`)) {
        return Promise.resolve(jsonResponse(activeServer));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${activeServer.id}`,
    });

    expect(await screen.findByRole('heading', { name: 'Docs MCP' })).toBeInTheDocument();
    expect(screen.getByText('ACTIVE')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Tools/i }));
    expect(await screen.findByText(/tools boom/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Docs MCP' })).toBeInTheDocument();
  });

  it('runs connection test, shows result, and refetches server metadata', async () => {
    let serverGets = 0;
    const updatedServer = {
      ...activeServer,
      last_healthy_at: '2026-09-04T08:00:00+00:00',
      negotiated_protocol_version: '2025-06-18',
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'POST' && url.includes('/connection-tests')) {
        return Promise.resolve(jsonResponse(succeededCheck, 201));
      }
      if (url.includes('/tools')) return Promise.resolve(jsonResponse(toolList));
      if (url.includes('/discoveries')) return Promise.resolve(jsonResponse(discoveryList));
      if (url.includes('/mcp/servers/') && !url.includes('/tools') && !url.includes('/discoveries')) {
        serverGets += 1;
        return Promise.resolve(jsonResponse(serverGets === 1 ? activeServer : updatedServer));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${activeServer.id}`,
    });

    await screen.findByText('Docs MCP');
    await user.click(screen.getByRole('button', { name: /연결 테스트/i }));

    expect(await screen.findByText(/Succeeded/i)).toBeInTheDocument();
    expect(screen.getByText(/42ms/)).toBeInTheDocument();
    await waitFor(() => {
      expect(serverGets).toBeGreaterThanOrEqual(2);
    });
    expect(await screen.findByText('2025-06-18')).toBeInTheDocument();
  });

  it('discovery preview then apply with confirm dialog and refetches', async () => {
    let serverGets = 0;
    const afterPreview = {
      ...activeServer,
      discovery_mode: 'EXPLICIT_DISCOVERY' as const,
      negotiated_protocol_version: '2025-06-18',
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      const body = init?.body ? JSON.parse(init.body as string) : {};
      if (init?.method === 'POST' && url.includes('/discoveries')) {
        if (body.apply_changes) {
          return Promise.resolve(jsonResponse(appliedDiscovery, 201));
        }
        return Promise.resolve(jsonResponse(previewDiscovery, 201));
      }
      if (url.includes('/tools')) return Promise.resolve(jsonResponse(toolList));
      if (url.includes('/discoveries')) return Promise.resolve(jsonResponse(discoveryList));
      if (url.includes('/mcp/servers/')) {
        serverGets += 1;
        return Promise.resolve(jsonResponse(serverGets === 1 ? activeServer : afterPreview));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${activeServer.id}`,
    });

    await screen.findByText('Docs MCP');
    await user.click(screen.getByRole('button', { name: /Discovery Preview/i }));
    expect(await screen.findByText(/Discovery Preview — Success/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(serverGets).toBeGreaterThanOrEqual(2);
    });

    await user.click(screen.getByRole('button', { name: /Apply Discovery/i }));
    await user.click(screen.getByRole('button', { name: /^Apply$/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/discoveries'),
        expect.objectContaining({
          method: 'POST',
          body: expect.stringContaining('"apply_changes":true'),
        }),
      );
    });
  });
});
