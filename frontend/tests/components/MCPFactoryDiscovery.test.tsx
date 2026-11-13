import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPServerDetail from '@/screens/mcp/MCPServerDetail';
import {
  draftServer, discoveryList, previewDiscovery, toolList,
} from '../fixtures/mcp-api';
import type { DiscoveryDto, FactoryDiscoveryDiffDto } from '@/api/types';
import { renderWithRouter } from '../test-utils';

const server = { ...draftServer, id: 'factory-server-001', name: 'Factory 도구 서버', endpoint_url: 'https://mcpflow.openlink.kr/mcp/factory/test' };
const clean: FactoryDiscoveryDiffDto = {
  status: 'CLEAN',
  matched: 2,
  missing_expected: 0,
  unexpected_remote: 0,
  contract_changed: 0,
  error_message: null,
  classifications: [
    { remote_name: 'getPet', classification: 'MATCHED', expected_contract_hash: null, remote_contract_hash: null },
    { remote_name: 'getPets', classification: 'MATCHED', expected_contract_hash: null, remote_contract_hash: null },
  ],
};
const mismatch: FactoryDiscoveryDiffDto = {
  ...clean,
  status: 'MISMATCHED',
  matched: 1,
  missing_expected: 1,
  unexpected_remote: 1,
  contract_changed: 1,
  error_message: 'Factory Discovery reconciliation failed.',
  classifications: [
    { remote_name: 'getPets', classification: 'MATCHED', expected_contract_hash: null, remote_contract_hash: null },
    { remote_name: 'getPet', classification: 'MISSING_EXPECTED', expected_contract_hash: 'a', remote_contract_hash: null },
    { remote_name: 'unknown_tool', classification: 'UNEXPECTED_REMOTE', expected_contract_hash: null, remote_contract_hash: 'b' },
    { remote_name: 'getPetV2', classification: 'CONTRACT_CHANGED', expected_contract_hash: 'c', remote_contract_hash: 'd' },
  ],
};

function respond(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}

function stubFactoryDiscovery(preview: DiscoveryDto, history?: DiscoveryDto[]) {
  return vi.fn((input: RequestInfo, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.url;
    if (url.includes('/discoveries') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body ?? '{}'));
      return Promise.resolve(respond(body.apply_changes
        ? { ...preview, apply_changes: true, id: 'factory-applied-1' }
        : preview, 201));
    }
    if (url.includes('/discoveries')) {
      return Promise.resolve(respond({ ...discoveryList, items: history ?? [] }));
    }
    if (url.includes('/tools')) return Promise.resolve(respond(toolList));
    if (url.includes('/mcp/servers/')) return Promise.resolve(respond(server));
    return Promise.resolve(respond({ error: { code: 'NOT_FOUND', message: 'Missing' } }, 404));
  });
}
function view() {
  return renderWithRouter(<MCPServerDetail />, {
    path: '/mcp/servers/:serverId',
    route: `/mcp/servers/${server.id}`,
  });
}

describe('Factory Discovery contract evidence and apply fail-closed', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders MISMATCHED classifications and blocks applying results', async () => {
    const fetchMock = stubFactoryDiscovery({
      ...previewDiscovery, mcp_server_id: server.id, success: false,
      error_code: 'FACTORY_DISCOVERY_MISMATCH', factory_diff: mismatch,
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    view();

    await screen.findByRole('heading', { name: 'Factory 도구 서버' });
    await user.click(screen.getByRole('button', { name: '도구 탐색 미리보기' }));

    const region = await screen.findByRole('region', { name: 'Factory Discovery 계약 정합성' });
    expect(region).toHaveTextContent('Factory 계약 불일치 (MISMATCHED)');
    expect(region).toHaveTextContent('getPet: 예상 도구 누락');
    expect(region).toHaveTextContent('unknown_tool: 미승인 원격 도구');
    expect(region).toHaveTextContent('getPetV2: 계약 변경');
    expect(screen.getByRole('button', { name: '탐색 결과 적용' })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([url, init]) =>
      String(url).includes('/discoveries') && init?.method === 'POST',
    )).toHaveLength(1);
  });

  it('renders AMBIGUOUS and blocks apply even when a legacy generic success flag is true', async () => {
    const ambiguous = { ...mismatch, status: 'AMBIGUOUS' as const, error_message: 'duplicate tool name' };
    const fetchMock = stubFactoryDiscovery({
      ...previewDiscovery, mcp_server_id: server.id, success: true,
      factory_diff: ambiguous,
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    view();

    await screen.findByRole('heading', { name: 'Factory 도구 서버' });
    await user.click(screen.getByRole('button', { name: '도구 탐색 미리보기' }));
    expect(await screen.findByText('Factory 계약 판정 불명확 (AMBIGUOUS)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '탐색 결과 적용' })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([url, init]) =>
      String(url).includes('/discoveries') && init?.method === 'POST',
    )).toHaveLength(1);
  });

  it('allows CLEAN apply only after user preview, confirmation, and Backend revalidation', async () => {
    const fetchMock = stubFactoryDiscovery({
      ...previewDiscovery, mcp_server_id: server.id, factory_diff: clean,
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    view();

    await screen.findByRole('heading', { name: 'Factory 도구 서버' });
    await user.click(screen.getByRole('button', { name: '도구 탐색 미리보기' }));
    expect(await screen.findByText('Factory 계약 정합성 일치 (CLEAN)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '탐색 결과 적용' })).toBeEnabled();
    expect(fetchMock.mock.calls.filter(([url, init]) =>
      String(url).includes('/discoveries') && init?.method === 'POST',
    )).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: '탐색 결과 적용' }));
    expect(fetchMock.mock.calls.filter(([url, init]) =>
      String(url).includes('/discoveries') && init?.method === 'POST',
    )).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: /^적용$/ }));
    await waitFor(() => {
      const writes = fetchMock.mock.calls.filter(([url, init]) =>
        String(url).includes('/discoveries') && init?.method === 'POST',
      );
      expect(writes).toHaveLength(2);
      expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ mode: 'FULL', apply_changes: false });
      expect(JSON.parse(String(writes[1][1]?.body))).toEqual({ mode: 'FULL', apply_changes: true });
    });
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes('/activate'))).toBe(true);
  });

  it('includes Factory reconciliation status in saved Discovery history', async () => {
    const prior = {
      ...previewDiscovery, mcp_server_id: server.id,
      success: false, factory_diff: mismatch,
    };
    const fetchMock = stubFactoryDiscovery(prior, [prior]);
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    view();
    await screen.findByRole('heading', { name: 'Factory 도구 서버' });
    await user.click(screen.getByRole('button', { name: /탐색 이력/ }));
    expect(await screen.findByText(/Factory 정합성: MISMATCHED/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([url, init]) =>
      !(String(url).includes('/discoveries') && init?.method === 'POST'),
    )).toBe(true);
  });

  it('non-Factory Discovery still follows the ordinary preview/apply contract', async () => {
    const fetchMock = stubFactoryDiscovery(previewDiscovery);
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    view();
    await screen.findByRole('heading', { name: 'Factory 도구 서버' });
    await user.click(screen.getByRole('button', { name: '도구 탐색 미리보기' }));
    expect(await screen.findByText('도구 탐색 미리보기 — 성공')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Factory Discovery 계약 정합성' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '탐색 결과 적용' })).toBeEnabled();
  });
});
