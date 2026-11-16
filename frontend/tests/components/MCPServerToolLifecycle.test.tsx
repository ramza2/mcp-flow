import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation, useNavigate } from 'react-router';
import MCPServerDetail from '@/screens/mcp/MCPServerDetail';
import { activeServer, activeTool, inactiveTool, missingTool, discoveryList } from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

const list = { items: [inactiveTool, activeTool], total: 2, page: 1, page_size: 20, has_next: false };
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
function mockApi(
  options: {
    tools?: typeof list.items;
    onWrite?: (url: string, init: RequestInit) => Response;
  } = {},
) {
  const tools = options.tools ?? list.items;
  return vi.fn((input: RequestInfo, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.url;
    const method = init?.method ?? 'GET';
    if (url.includes('/mcp/tools/') && method === 'POST') {
      if (options.onWrite) return Promise.resolve(options.onWrite(url, init!));
      const row = tools.find(x => url.includes(x.id))!;
      return Promise.resolve(json({
        ...row,
        status: url.endsWith('/activate') ? 'ACTIVE' : 'INACTIVE',
        lock_version: row.lock_version + 1,
      }));
    }
    if (url.includes('/mcp/servers/') && url.endsWith('/tools')) {
      return Promise.resolve(json({ ...list, items: tools, total: tools.length }));
    }
    if (url.includes('/discoveries')) return Promise.resolve(json(discoveryList));
    if (url.includes('/mcp/servers/')) return Promise.resolve(json(activeServer));
    return Promise.resolve(json({ error: { code: 'NOT_FOUND', message: 'Not found' } }, 404));
  });
}
const route = `/mcp/servers/${activeServer.id}`;
function view(fetchMock: ReturnType<typeof mockApi>, start = `${route}?tab=tools`) {
  vi.stubGlobal('fetch', fetchMock);
  return renderWithRouter(<MCPServerDetail />, { route: start, path: '/mcp/servers/:serverId' });
}

describe('MCP server tool list: inline lifecycle and tab preservation', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('activates one INACTIVE row only after explicit confirmation, stays on Tools tab, and sends If-Match', async () => {
    const fetchMock = mockApi();
    const user = userEvent.setup();
    const { router } = view(fetchMock);
    expect(await screen.findByRole('button', { name: '도구 활성화: Search Docs' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '도구 활성화: Search Docs' }));
    expect(screen.getByText('MCP 도구 활성화')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url, init]) =>
      String(url).endsWith('/activate') && init?.method === 'POST',
    )).toBe(false);

    await user.click(screen.getByRole('button', { name: '도구 활성화 확인' }));
    expect(await screen.findByRole('button', { name: '도구 비활성화: Search Docs' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(route);
    expect(router.state.location.search).toBe('?tab=tools');
    const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe(`/api/v1/mcp/tools/${inactiveTool.id}/activate`);
    expect((writes[0][1]?.headers as Record<string, string>)['If-Match']).toBe(String(inactiveTool.lock_version));
  });

  it('can cancel without POST and deactivates ACTIVE row with separate confirmation', async () => {
    const fetchMock = mockApi();
    const user = userEvent.setup();
    view(fetchMock);
    await screen.findByRole('button', { name: '도구 비활성화: Search Docs' });
    await user.click(screen.getByRole('button', { name: '도구 비활성화: Search Docs' }));
    await user.click(screen.getByRole('button', { name: '취소' }));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
    await user.click(screen.getByRole('button', { name: '도구 비활성화: Search Docs' }));
    await user.click(screen.getByRole('button', { name: '도구 비활성화 확인' }));
    expect(await screen.findByRole('button', { name: '도구 활성화: Search Docs' })).toBeInTheDocument();
    const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe(`/api/v1/mcp/tools/${activeTool.id}/deactivate`);
  });

  it('does not offer lifecycle actions for MISSING/BLOCKED rows', async () => {
    const fetchMock = mockApi({ tools: [
      missingTool,
      { ...inactiveTool, status: 'BLOCKED' as const },
    ] });
    view(fetchMock);
    expect(await screen.findAllByText('상태 변경 불가')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: /도구 활성화:|도구 비활성화:/ })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
  });

  it('shows version-conflict and refreshes list instead of retrying the mutation', async () => {
    const fetchMock = mockApi({
      onWrite: () => json({ error: {
        code: 'RESOURCE_VERSION_CONFLICT',
        message: 'Another update happened',
        request_id: 'req-inline-tool-conflict',
      } }, 409),
    });
    const user = userEvent.setup();
    view(fetchMock);
    await screen.findByRole('button', { name: '도구 활성화: Search Docs' });
    await user.click(screen.getByRole('button', { name: '도구 활성화: Search Docs' }));
    await user.click(screen.getByRole('button', { name: '도구 활성화 확인' }));
    expect(await screen.findByText('Another update happened')).toBeInTheDocument();
    expect(screen.getByText('Request ID: req-inline-tool-conflict')).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url, init]) =>
      String(url).endsWith('/tools') && init?.method === 'GET',
    ).length).toBeGreaterThanOrEqual(2));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it('stores active tab in URL, preserves it through navigation and browser Back', async () => {
    function ToolLanding() {
      const navigate = useNavigate();
      const location = useLocation();
      return <div>
        <p>도구 상세</p>
        <p>{(location.state as {fromServer?: boolean}|null)?.fromServer ? '서버에서 이동' : '일반 이동'}</p>
        <button onClick={() => navigate(-1)}>뒤로</button>
      </div>;
    }
    const fetchMock = mockApi();
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    const { router } = renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route,
      routes: [
        { path: '/mcp/servers/:serverId', element: <MCPServerDetail /> },
        { path: '/mcp/tools/:toolId', element: <ToolLanding /> },
      ],
    });
    await screen.findByRole('heading', { name: 'Docs MCP' });
    await user.click(screen.getByRole('button', { name: /^도구\s*\d+$/ }));
    expect(router.state.location.search).toBe('?tab=tools');
    await user.click(screen.getByText('Search Docs'));
    expect(await screen.findByText('도구 상세')).toBeInTheDocument();
    expect(screen.getByText('서버에서 이동')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '뒤로' }));
    expect(await screen.findByRole('button', { name: '도구 활성화: Search Docs' })).toBeInTheDocument();
    expect(router.state.location.search).toBe('?tab=tools');
  });
});
