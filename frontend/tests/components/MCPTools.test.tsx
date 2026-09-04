import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPTools from '@/screens/mcp/MCPTools';
import { discoveredTool, serverList, toolList } from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCPTools — API list', () => {
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
            jsonResponse({ items: [discoveredTool], page: 1, page_size: 20, total: 1, has_next: false }),
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
      expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('status=DISCOVERED'), expect.any(Object));
    });
  });
});
