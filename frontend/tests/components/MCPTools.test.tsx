import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPTools from '@/screens/mcp/MCPTools';
import {
  activeTool,
  blockedTool,
  discoveredTool,
  inactiveTool,
  missingTool,
  serverList,
  toolList,
} from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCPTools — API list + lifecycle', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('loads tools and server filter options', async () => {
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/mcp/tools')) return Promise.resolve(jsonResponse(toolList));
      if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });

    expect(await screen.findByText('Search Docs')).toBeInTheDocument();
    expect(screen.getAllByText('Docs MCP').length).toBeGreaterThan(0);
  });

  it('shows Activate for DISCOVERED/INACTIVE and Deactivate for ACTIVE; disables MISSING/BLOCKED', async () => {
    const list = {
      items: [discoveredTool, activeTool, inactiveTool, missingTool, blockedTool],
      page: 1,
      page_size: 20,
      total: 5,
      has_next: false,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/mcp/tools')) return Promise.resolve(jsonResponse(list));
        if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });
    expect((await screen.findAllByText('Search Docs')).length).toBeGreaterThan(0);

    expect(screen.getAllByRole('button', { name: 'Activate' }).length).toBe(2);
    expect(screen.getByRole('button', { name: 'Deactivate' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '—' }).length).toBe(2);
  });

  it('Activate sends If-Match with latest lock_version and updates row', async () => {
    const user = userEvent.setup();
    const activated = { ...discoveredTool, status: 'ACTIVE', lock_version: 2 };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/activate') && init?.method === 'POST') {
        expect(init.headers).toEqual(expect.objectContaining({ 'If-Match': '1' }));
        return Promise.resolve(jsonResponse(activated));
      }
      if (url.includes('/mcp/tools') && !url.includes('/activate')) {
        return Promise.resolve(
          jsonResponse({
            items: [discoveredTool],
            page: 1,
            page_size: 20,
            total: 1,
            has_next: false,
          }),
        );
      }
      if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });
    await screen.findByText('Search Docs');
    await user.click(screen.getByRole('button', { name: 'Activate' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Deactivate' })).toBeInTheDocument();
    });
  });

  it('409 RESOURCE_VERSION_CONFLICT shows feedback and refetches tool', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/activate') && init?.method === 'POST') {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'RESOURCE_VERSION_CONFLICT',
                message: 'lock mismatch',
                request_id: 'req-conflict',
              },
            },
            409,
          ),
        );
      }
      if (url.match(/\/mcp\/tools\/[^/?]+$/) && (!init || !init.method || init.method === 'GET')) {
        return Promise.resolve(
          jsonResponse({ ...discoveredTool, lock_version: 9, status: 'INACTIVE' }),
        );
      }
      if (url.includes('/mcp/tools')) {
        return Promise.resolve(
          jsonResponse({
            items: [discoveredTool],
            page: 1,
            page_size: 20,
            total: 1,
            has_next: false,
          }),
        );
      }
      if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });
    await screen.findByText('Search Docs');
    await user.click(screen.getByRole('button', { name: 'Activate' }));

    expect(await screen.findByText(/다른 작업으로 Tool 상태가 변경되었습니다/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Activate' })).toBeInTheDocument();
    });
  });

  it('shows error state on failure', async () => {
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/mcp/tools')) {
        return Promise.resolve(
          jsonResponse({ error: { code: 'ERR', message: 'tools failed' } }, 500),
        );
      }
      if (url.includes('/mcp/servers')) {
        return Promise.resolve(jsonResponse(serverList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });

    expect(await screen.findByText('tools failed')).toBeInTheDocument();
  });

  it('shows empty state when no tools', async () => {
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/mcp/tools')) {
        return Promise.resolve(
          jsonResponse({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
        );
      }
      if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });

    expect(await screen.findByText(/등록된 MCP Tool이 없습니다/i)).toBeInTheDocument();
  });

  it('passes status filter to API', async () => {
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/mcp/tools')) {
        if (url.includes('status=DISCOVERED')) {
          return Promise.resolve(
            jsonResponse({
              items: [discoveredTool],
              page: 1,
              page_size: 20,
              total: 1,
              has_next: false,
            }),
          );
        }
        return Promise.resolve(jsonResponse(toolList));
      }
      if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });
    await screen.findByText('Search Docs');

    const selects = screen.getAllByRole('combobox');
    await user.selectOptions(selects[0], 'DISCOVERED');

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('status=DISCOVERED'),
        expect.any(Object),
      );
    });
  });

  it('tracks per-tool mutation pending independently for concurrent A/B lifecycle', async () => {
    const user = userEvent.setup();
    let resolveActivate!: (value: Response) => void;
    let resolveDeactivate!: (value: Response) => void;
    const pendingActivate = new Promise<Response>(resolve => {
      resolveActivate = resolve;
    });
    const pendingDeactivate = new Promise<Response>(resolve => {
      resolveDeactivate = resolve;
    });

    const toolA = { ...discoveredTool, display_name: 'Tool A Docs' };
    const toolB = { ...activeTool, display_name: 'Tool B Active' };
    const list = {
      items: [toolA, toolB],
      page: 1,
      page_size: 20,
      total: 2,
      has_next: false,
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes(`/mcp/tools/${toolA.id}/activate`) && init?.method === 'POST') {
        return pendingActivate;
      }
      if (url.includes(`/mcp/tools/${toolB.id}/deactivate`) && init?.method === 'POST') {
        return pendingDeactivate;
      }
      if (url.includes('/mcp/tools')) return Promise.resolve(jsonResponse(list));
      if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });
    await screen.findByText('Tool A Docs');
    await screen.findByText('Tool B Active');

    const activateBtn = screen.getByRole('button', { name: 'Activate' });
    const deactivateBtn = screen.getByRole('button', { name: 'Deactivate' });
    await user.click(activateBtn);
    await user.click(deactivateBtn);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Activate' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Deactivate' })).toBeDisabled();
    });

    resolveActivate(jsonResponse({ ...toolA, status: 'ACTIVE', lock_version: 2 }));

    // A finished → its new Deactivate is enabled; B's pending Deactivate stays disabled.
    await waitFor(() => {
      const deactivateButtons = screen.getAllByRole('button', { name: 'Deactivate' });
      expect(deactivateButtons).toHaveLength(2);
      expect(deactivateButtons.filter(btn => (btn as HTMLButtonElement).disabled)).toHaveLength(1);
      expect(deactivateButtons.filter(btn => !(btn as HTMLButtonElement).disabled)).toHaveLength(1);
    });

    resolveDeactivate(jsonResponse({ ...toolB, status: 'INACTIVE', lock_version: 3 }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Deactivate' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Activate' })).toBeEnabled();
    });
  });

  it('ignores duplicate clicks on the same tool while mutation is pending', async () => {
    const user = userEvent.setup();
    let resolveActivate!: (value: Response) => void;
    const pendingActivate = new Promise<Response>(resolve => {
      resolveActivate = resolve;
    });
    let activatePosts = 0;

    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/activate') && init?.method === 'POST') {
        activatePosts += 1;
        return pendingActivate;
      }
      if (url.includes('/mcp/tools')) {
        return Promise.resolve(
          jsonResponse({
            items: [discoveredTool],
            page: 1,
            page_size: 20,
            total: 1,
            has_next: false,
          }),
        );
      }
      if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPTools />, { path: '/mcp/tools', route: '/mcp/tools' });
    await screen.findByText('Search Docs');

    const btn = screen.getByRole('button', { name: 'Activate' });
    await user.click(btn);
    await user.click(btn);
    await user.click(btn);

    await waitFor(() => expect(activatePosts).toBe(1));
    expect(activatePosts).toBe(1);

    resolveActivate(jsonResponse({ ...discoveredTool, status: 'ACTIVE', lock_version: 2 }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Deactivate' })).toBeEnabled();
    });
    expect(activatePosts).toBe(1);
  });
});
