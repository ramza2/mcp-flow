import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FactoryRuntimeBindingPanel from '@/screens/mcp/FactoryRuntimeBindingPanel';
import { renderWithRouter } from '../test-utils';

const PUBLISH_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const SERVER_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const BINDING_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';

const draft = {
  id: BINDING_ID,
  publish_id: PUBLISH_ID,
  mcp_server_id: SERVER_ID,
  review_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
  generated_package_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
  package_content_sha256: 'a'.repeat(64),
  status: 'DRAFT',
  base_url: null,
  auth_type: 'NONE',
  auth_secret_id: null,
  connect_timeout_ms: 5000,
  call_timeout_ms: 10000,
  max_response_bytes: 1048576,
  mcp_endpoint_url: null,
  last_error_code: null,
  last_error_message: null,
  created_by: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
  lock_version: 1,
  created_at: '2026-11-13T00:00:00Z',
  updated_at: '2026-11-13T00:00:00Z',
};
const notFound = { error: { code: 'NOT_FOUND', message: '연결 설정 없음' } };
const ready = {
  ...draft, status: 'READY', base_url: 'https://api.example.com',
  lock_version: 3, mcp_endpoint_url: `https://mcpflow.openlink.kr/mcp/factory/${BINDING_ID}`,
};

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}
function view() {
  return renderWithRouter(
    <FactoryRuntimeBindingPanel publishId={PUBLISH_ID} mcpServerId={SERVER_ID} />,
    { route: '/tool-factory/reviews/123', path: '/tool-factory/reviews/:reviewId' },
  );
}

describe('Factory RuntimeBinding onboarding', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('only GETs on first render; user must explicitly consent before creating', async () => {
    const mock = vi.fn((_url: string, _opts: RequestInit) => Promise.resolve(json(notFound, 404)));
    vi.stubGlobal('fetch', mock);
    view();

    expect(await screen.findByText('연결 설정이 아직 없습니다')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'DRAFT 연결 설정 생성' })).toBeDisabled();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
    expect(mock.mock.calls[0][0]).toBe(
      `/api/v1/factory/publishes/${PUBLISH_ID}/runtime-binding`,
    );
  });

  it('creates only on explicit consent; server returns a persisted DRAFT Binding', async () => {
    const mock = vi.fn((_url: string, opts: RequestInit) => Promise.resolve(
      opts.method === 'POST' ? json(draft, 201) : json(notFound, 404),
    ));
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    view();

    await screen.findByText('연결 설정이 아직 없습니다');
    await user.click(screen.getByRole('checkbox', { name: /운영 DB에 DRAFT Runtime Binding/ }));
    await user.click(screen.getByRole('button', { name: 'DRAFT 연결 설정 생성' }));
    expect(await screen.findByText(BINDING_ID)).toBeInTheDocument();

    const posts = mock.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe(
      `/api/v1/factory/publishes/${PUBLISH_ID}/runtime-bindings`,
    );
    expect((posts[0][1].headers as Record<string, string>)['X-CSRF-Token']).toBe('test-csrf-token');
    expect(screen.getByRole('button', { name: 'Runtime Binding READY 전환' })).toBeDisabled();
  });

  it('persists REST upstream, requires a separate READY confirmation and optimistic lock', async () => {
    const saved = { ...draft, base_url: 'https://api.example.com', lock_version: 2 };
    const mock = vi.fn((_url: string, opts: RequestInit) => Promise.resolve(
      opts.method === 'PATCH' ? json(saved)
        : opts.method === 'POST' ? json(ready) : json(draft),
    ));
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    view();

    const field = await screen.findByRole('textbox', { name: '원본 REST API Base URL' });
    await user.type(field, 'https://api.example.com');
    expect(screen.getByRole('button', { name: 'Runtime Binding READY 전환' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '연결 설정 저장' }));
    await waitFor(() => expect(screen.getByText('2')).toBeInTheDocument());

    const patches = mock.mock.calls.filter(([, opts]) => opts.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0][0]).toBe(`/api/v1/factory/runtime-bindings/${BINDING_ID}`);
    expect(JSON.parse(patches[0][1].body as string)).toEqual({
      base_url: 'https://api.example.com',
      auth_type: 'NONE',
      auth_secret_id: null,
      lock_version: 1,
    });
    expect((patches[0][1].headers as Record<string, string>)['X-CSRF-Token']).toBe('test-csrf-token');

    const readyButton = screen.getByRole('button', { name: 'Runtime Binding READY 전환' });
    expect(readyButton).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /상위 API 연결 설정이 저장되었으며 READY/ }));
    await user.click(readyButton);
    expect(await screen.findByText(/Runtime Binding이 READY입니다/)).toBeInTheDocument();

    const posts = mock.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe(`/api/v1/factory/runtime-bindings/${BINDING_ID}/ready`);
    expect(JSON.parse(posts[0][1].body as string)).toEqual({ lock_version: 2 });
    expect((posts[0][1].headers as Record<string, string>)['X-CSRF-Token']).toBe('test-csrf-token');
    expect(screen.getAllByText(ready.mcp_endpoint_url).length).toBeGreaterThanOrEqual(1);
  });

  it('never allows READY without a saved upstream URL', async () => {
    vi.stubGlobal('fetch', vi.fn((_url: string, _opts: RequestInit) => Promise.resolve(json(draft))));
    view();
    await screen.findByText(BINDING_ID);
    expect(screen.getByRole('button', { name: 'Runtime Binding READY 전환' })).toBeDisabled();
  });

  it('uses an existing Secret UUID reference and never requests raw secret credentials', async () => {
    const mock = vi.fn((_url: string, opts: RequestInit) =>
      Promise.resolve(opts.method === 'PATCH'
        ? json({ ...draft, auth_type: 'BEARER', auth_secret_id: '11111111-1111-4111-8111-111111111111', base_url: 'https://api.example.com', lock_version: 2 })
        : json(draft)),
    );
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    view();
    await screen.findByText(BINDING_ID);
    await user.type(screen.getByRole('textbox', { name: '원본 REST API Base URL' }), 'https://api.example.com');
    await user.selectOptions(screen.getByRole('combobox', { name: '상위 API 인증' }), 'BEARER');
    expect(screen.getByText(/실제 비밀번호·API Key·토큰은 입력하지 마세요/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '연결 설정 저장' })).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '기존 Secret ID' }), '11111111-1111-4111-8111-111111111111');
    await user.click(screen.getByRole('button', { name: '연결 설정 저장' }));
    const patches = mock.mock.calls.filter(([, opts]) => opts.method === 'PATCH');
    expect(patches).toHaveLength(1);
    const body = JSON.parse(patches[0][1].body as string);
    expect(body.auth_type).toBe('BEARER');
    expect(body.auth_secret_id).toBe('11111111-1111-4111-8111-111111111111');
    expect(JSON.stringify(body)).not.toContain('password');
  });

  it('READY remains read-only and checks the actual MCP server endpoint with GET', async () => {
    const linkedServer = {
      id: SERVER_ID, status: 'DRAFT', endpoint_url: ready.mcp_endpoint_url,
      lock_version: 4,
    };
    const mock = vi.fn((url: string, _opts: RequestInit) =>
      Promise.resolve(json(url.includes('/mcp/servers/') ? linkedServer : ready)),
    );
    vi.stubGlobal('fetch', mock);
    view();
    expect(await screen.findByText(/Runtime Binding이 READY입니다/)).toBeInTheDocument();
    expect(await screen.findByText(/생성된 MCP 엔드포인트가 서버에 등록되었습니다/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '연결 테스트·도구 탐색 화면으로' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '연결 설정 저장' })).not.toBeInTheDocument();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });
});
