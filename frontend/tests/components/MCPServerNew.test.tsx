import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPServerNew from '@/screens/mcp/MCPServerNew';
import { CURRENT_MCP_PROTOCOL_VERSION } from '@/domain';
import { draftServer } from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function footerNext(container: HTMLElement): HTMLButtonElement {
  const buttons = within(container).getAllByRole('button', { name: /^다음$/ });
  const enabled = buttons.find(b => !(b as HTMLButtonElement).disabled) as HTMLButtonElement;
  if (!enabled) throw new Error('No enabled Next button');
  return enabled;
}

describe('MCPServerNew — API wizard', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('guides through 3 honest steps and requires the name and endpoint', async () => {
    const user = userEvent.setup();
    const view = renderWithRouter(<MCPServerNew />, {
      path: '/mcp/servers/new',
      route: '/mcp/servers/new',
    });

    expect(screen.getByRole('heading', { name: 'MCP 서버 등록' })).toBeInTheDocument();
    expect(footerNext(view.container)).toBeDisabled();

    await user.type(screen.getByRole('textbox', { name: '서버 이름' }), 'Test MCP');
    await user.click(footerNext(view.container));

    expect(screen.getByText(/Streamable HTTP \(현재 지원\)/)).toBeInTheDocument();
    expect(footerNext(view.container)).toBeDisabled();
    expect(screen.getByText(/토큰이나 API 키가 필요한 서버는 여기서 연결할 수 없으며/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '연결 테스트 실행' })).not.toBeInTheDocument();

    await user.type(screen.getByRole('textbox', { name: 'MCP 서버 주소 (URL)' }), 'https://example.com/mcp');
    await user.click(footerNext(view.container));

    expect(screen.getByText(new RegExp(CURRENT_MCP_PROTOCOL_VERSION))).toBeInTheDocument();
    expect(screen.getByText(/등록 후 서버 상세 화면으로 이동합니다/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '서버 등록' })).toBeEnabled();
  });

  it('POST createMCPServer and navigates to detail on success', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/mcp/servers') && !url.match(/\/mcp\/servers\//)) {
        return Promise.resolve(jsonResponse(draftServer, 201));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = renderWithRouter(<MCPServerNew />, {
      path: '/mcp/servers/new',
      route: '/mcp/servers/new',
      routes: [
        { path: '/mcp/servers/new', element: <MCPServerNew /> },
        { path: '/mcp/servers/:serverId', element: <div>Server Detail</div> },
      ],
    });

    await user.type(screen.getByPlaceholderText('Weather MCP'), 'Test MCP');
    await user.click(footerNext(view.container));
    await user.type(screen.getByPlaceholderText('https://mcp.example.com/mcp'), 'https://example.com/mcp');

    await user.click(footerNext(view.container));

    await user.click(screen.getByRole('button', { name: /서버 등록/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/v1/mcp/servers'),
        expect.objectContaining({ method: 'POST' }),
      );
    });

    await waitFor(() => {
      expect(screen.getByText('Server Detail')).toBeInTheDocument();
    });
  });

  it('shows 422 error and keeps form on validation failure', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(
          { error: { code: 'VALIDATION_ERROR', message: 'name is required', request_id: 'req-1' } },
          422,
        ),
      ),
    );

    const view = renderWithRouter(<MCPServerNew />, {
      path: '/mcp/servers/new',
      route: '/mcp/servers/new',
    });

    await user.type(screen.getByPlaceholderText('Weather MCP'), 'Bad');
    await user.click(footerNext(view.container));
    await user.type(screen.getByPlaceholderText('https://mcp.example.com/mcp'), 'https://example.com/mcp');

    await user.click(footerNext(view.container));

    await user.click(screen.getByRole('button', { name: /서버 등록/i }));
    expect(await screen.findByText('name is required')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /서버 등록/i })).toBeInTheDocument();
  });
});
