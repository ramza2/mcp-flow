import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FactoryMCPServerEndpointLink from '@/screens/mcp/FactoryMCPServerEndpointLink';
import type { FactoryRuntimeBindingDto } from '@/api/factory';
import { renderWithRouter } from '../test-utils';

const SERVER_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const BINDING_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const ENDPOINT = `https://mcpflow.openlink.kr/mcp/factory/${BINDING_ID}`;

const binding: FactoryRuntimeBindingDto = {
  id: BINDING_ID,
  publish_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  mcp_server_id: SERVER_ID,
  review_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
  generated_package_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
  package_content_sha256: 'a'.repeat(64),
  status: 'READY',
  base_url: 'https://api.example.com',
  auth_type: 'NONE',
  auth_secret_id: null,
  connect_timeout_ms: 5000,
  call_timeout_ms: 10000,
  max_response_bytes: 1048576,
  mcp_endpoint_url: ENDPOINT,
  last_error_code: null,
  last_error_message: null,
  created_by: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
  lock_version: 2,
  created_at: '2026-11-13T00:00:00Z',
  updated_at: '2026-11-13T00:01:00Z',
};
const server = {
  id: SERVER_ID, name: 'Factory DRAFT Server',
  endpoint_url: null as string | null, status: 'DRAFT',
  transport_type: 'STREAMABLE_HTTP', lock_version: 4,
};

function respond(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
function view(props = binding) {
  return renderWithRouter(<FactoryMCPServerEndpointLink binding={props} />, {
    route: '/tool-factory/reviews/123',
    path: '/tool-factory/reviews/:reviewId',
  });
}

describe('Factory MCP endpoint registration gate', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('GETs current server only; does not register endpoint without explicit confirmation', async () => {
    const fetchMock = vi.fn((_url: string, _options: RequestInit) => Promise.resolve(respond(server)));
    vi.stubGlobal('fetch', fetchMock);
    view();

    expect(await screen.findByText(/MCP 서버 상태:/)).toBeInTheDocument();
    expect(screen.getByText(ENDPOINT)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'MCP 서버 주소 등록' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '연결 테스트·도구 탐색 화면으로' })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls).toHaveLength(1);
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/v1/mcp/servers/${SERVER_ID}`);
    expect(fetchMock.mock.calls[0][1].method).toBe('GET');
  });

  it('PATCHes only once with the exact READY endpoint, If-Match lock and CSRF; never automatically tests or activates', async () => {
    const updated = { ...server, endpoint_url: ENDPOINT, lock_version: 5 };
    const fetchMock = vi.fn((_url: string, opts: RequestInit) => Promise.resolve(
      opts.method === 'PATCH' ? respond(updated) : respond(server),
    ));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    view();

    await screen.findByRole('button', { name: 'MCP 서버 주소 등록' });
    await user.click(screen.getByRole('checkbox', { name: /위 주소를 실제 MCP 서버에 등록/ }));
    await user.click(screen.getByRole('button', { name: 'MCP 서버 주소 등록' }));
    expect(await screen.findByText(/생성된 MCP 엔드포인트가 서버에 등록되었습니다/)).toBeInTheDocument();
    const writes = fetchMock.mock.calls.filter(([, opts]) => opts.method !== 'GET');
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe(`/api/v1/mcp/servers/${SERVER_ID}`);
    expect(writes[0][1].method).toBe('PATCH');
    expect(JSON.parse(writes[0][1].body as string)).toEqual({ endpoint_url: ENDPOINT });
    const headers = writes[0][1].headers as Record<string, string>;
    expect(headers['If-Match']).toBe('4');
    expect(headers['X-CSRF-Token']).toBe('test-csrf-token');
    expect(fetchMock.mock.calls.every(([url]) => !/connection-tests|discoveries|activate/.test(url))).toBe(true);
    expect(screen.getByRole('button', { name: '연결 테스트·도구 탐색 화면으로' })).toBeInTheDocument();
  });

  it('treats already matching endpoint as complete and never PATCHes', async () => {
    const mock = vi.fn((_url: string, _opts: RequestInit) => Promise.resolve(respond({ ...server, endpoint_url: ENDPOINT })));
    vi.stubGlobal('fetch', mock);
    view();
    expect(await screen.findByText(/생성된 MCP 엔드포인트가 서버에 등록되었습니다/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'MCP 서버 주소 등록' })).not.toBeInTheDocument();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('fails closed when the server already has a different MCP endpoint', async () => {
    const mock = vi.fn((_url: string, _opts: RequestInit) =>
      Promise.resolve(respond({ ...server, endpoint_url: 'https://different.example.com/mcp' })),
    );
    vi.stubGlobal('fetch', mock);
    view();
    expect(await screen.findByText(/다른 엔드포인트가 이미 등록돼 있습니다/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'MCP 서버 주소 등록' })).not.toBeInTheDocument();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('fails closed on server ID mismatch and never permits PATCH', async () => {
    const mock = vi.fn((_url: string, _opts: RequestInit) =>
      Promise.resolve(respond({ ...server, id: '11111111-1111-4111-8111-111111111111' })),
    );
    vi.stubGlobal('fetch', mock);
    view();
    expect(await screen.findByText(/서버 ID와 Factory Binding의 서버 ID가 일치하지 않습니다/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'MCP 서버 주소 등록' })).not.toBeInTheDocument();
  });

  it('does not allow PATCH to ACTIVE server or invalid READY endpoint', async () => {
    const mock = vi.fn((_url: string, _opts: RequestInit) =>
      Promise.resolve(respond({ ...server, status: 'ACTIVE' })),
    );
    vi.stubGlobal('fetch', mock);
    view();
    expect(await screen.findByText(/DRAFT 또는 INACTIVE 서버에만 주소 등록이 가능합니다/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'MCP 서버 주소 등록' })).not.toBeInTheDocument();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('shows Backend If-Match conflict with Request ID and requires a fresh GET', async () => {
    const error = { error: {
      code: 'RESOURCE_VERSION_CONFLICT',
      message: '다른 사용자가 서버 설정을 변경했습니다.',
      request_id: 'req-version-conflict',
    } };
    const mock = vi.fn((_url: string, opts: RequestInit) =>
      Promise.resolve(opts.method === 'PATCH' ? respond(error, 409) : respond(server)),
    );
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    view();

    await screen.findByRole('button', { name: 'MCP 서버 주소 등록' });
    await user.click(screen.getByRole('checkbox', { name: /위 주소를 실제 MCP 서버에 등록/ }));
    await user.click(screen.getByRole('button', { name: 'MCP 서버 주소 등록' }));
    expect(await screen.findByText('다른 사용자가 서버 설정을 변경했습니다.')).toBeInTheDocument();
    expect(screen.getByText('Request ID: req-version-conflict')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'MCP 서버 정보 다시 조회' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'MCP 서버 정보 다시 조회' }));
    await waitFor(() => expect(mock.mock.calls.filter(([, opts]) => opts.method === 'GET')).toHaveLength(2));
    expect(mock.mock.calls.filter(([, opts]) => opts.method === 'PATCH')).toHaveLength(1);
  });

  it('skips all API access when Binding is not READY', () => {
    const mock = vi.fn();
    vi.stubGlobal('fetch', mock);
    view({ ...binding, status: 'DRAFT', mcp_endpoint_url: null });
    expect(mock).not.toHaveBeenCalled();
    expect(screen.getByText(/Backend가 유효한 MCP 엔드포인트 URL을 반환하지 않아 등록할 수 없습니다/)).toBeInTheDocument();
  });
});
