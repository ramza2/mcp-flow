import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ToolTestCallTab from '@/screens/mcp/ToolTestCallTab';
import { validVersion } from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

const TOOL_ID = validVersion.mcp_tool_id;
const EXECUTION_ID = '11111111-1111-4111-8111-111111111111';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}

function view(fetchMock: ReturnType<typeof vi.fn>, onShowPolicy = vi.fn()) {
  vi.stubGlobal('fetch', fetchMock);
  const { router } = renderWithRouter(
    <ToolTestCallTab
      toolId={TOOL_ID}
      selectedVersion={validVersion}
      currentVersionId={validVersion.id}
      active
      onShowPolicy={onShowPolicy}
    />,
    {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${TOOL_ID}`,
      routes: [
        {
          path: '/mcp/tools/:toolId',
          element: <ToolTestCallTab
            toolId={TOOL_ID}
            selectedVersion={validVersion}
            currentVersionId={validVersion.id}
            active
            onShowPolicy={onShowPolicy}
          />,
        },
        { path: '/admin/roles', element: <div>역할 및 권한 관리</div> },
        { path: '/admin/users', element: <div>사용자 관리</div> },
      ],
    },
  );
  return { router, onShowPolicy };
}

describe('Manual tool test preflight guidance', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('guides ToolPolicy missing to current tool policy tab without creating a policy', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      json({ error: {
        code: 'EXECUTION_PRECONDITION_FAILED',
        message: 'MCPToolPolicy 없음.',
        request_id: 'req-policy-missing',
      } }, 409),
    );
    const user = userEvent.setup();
    const { onShowPolicy } = view(fetchMock);
    await user.click(screen.getByRole('button', { name: '테스트 실행' }));
    expect(await screen.findByText('MCPToolPolicy 없음.')).toBeInTheDocument();
    expect(screen.getByText('Request ID: req-policy-missing')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '이 도구의 실행 정책 설정으로 이동' }));
    expect(onShowPolicy).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(0);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it('offers least-privilege routes on 403 but never automatically grants access', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      json({ error: {
        code: 'FORBIDDEN',
        message: 'mcp.tool.execute + MCP_TOOL ResourceGrant 필요.',
        request_id: 'req-permission-missing',
      } }, 403),
    );
    const user = userEvent.setup();
    const { router } = view(fetchMock);
    await user.click(screen.getByRole('button', { name: '테스트 실행' }));
    expect(await screen.findByText(/도구 실행 권한이 없습니다/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '역할 기능 권한 확인' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '사용자별 도구 접근 권한 확인' }));
    expect(await screen.findByText('사용자 관리')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/admin/users');
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it('does not show policy setup for an unrelated 409', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ error: {
      code: 'EXECUTION_PRECONDITION_FAILED',
      message: 'MCP Server ACTIVE 실패.',
    } }, 409));
    const user = userEvent.setup();
    view(fetchMock);
    await user.click(screen.getByRole('button', { name: '테스트 실행' }));
    expect(await screen.findByText('MCP Server ACTIVE 실패.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '이 도구의 실행 정책 설정으로 이동' })).not.toBeInTheDocument();
  });

  it('describes terminal FAILED responses accurately rather than saying still running', async () => {
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/result')) {
        return Promise.resolve(json({
          execution_id: EXECUTION_ID, status: 'FAILED', available: false,
          text_items: [], message_echoed: null, truncated: false,
          unavailable_reason: 'NOT_SUCCEEDED',
        }));
      }
      if (url.includes('/test-calls') && init?.method === 'POST') {
        return Promise.resolve(json({
          id: EXECUTION_ID, status: 'CREATED', source_type: 'MANUAL_TOOL_TEST',
          trigger_type: 'TEST', mcp_tool_id: TOOL_ID,
          mcp_tool_version_id: validVersion.id, plan_hash: 'a'.repeat(64),
          requested_at: '2026-11-16T00:00:00Z', step_count: 1,
        }, 201));
      }
      return Promise.resolve(json({ error: { code: 'NOT_FOUND', message: 'missing' } }, 404));
    });
    const user = userEvent.setup();
    view(fetchMock);
    await user.click(screen.getByRole('button', { name: '테스트 실행' }));
    await screen.findByText(/실행 작업 생성됨/);
    await user.click(screen.getByRole('button', { name: '실행 응답 확인 / 새로고침' }));
    expect(await screen.findByText(/실행이 실패 또는 제한시간 초과로 종료됐습니다/)).toBeInTheDocument();
    expect(screen.queryByText(/아직 실행 중이거나/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '실행 상세로 이동' })).toBeInTheDocument();
  });

  it('continues to show safe response text on success without changing permissions', async () => {
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/result')) {
        return Promise.resolve(json({
          execution_id: EXECUTION_ID, status: 'SUCCEEDED', available: true,
          text_items: ['HTTP 200'], message_echoed: null, truncated: false,
          unavailable_reason: null,
        }));
      }
      if (url.includes('/test-calls') && init?.method === 'POST') {
        return Promise.resolve(json({
          id: EXECUTION_ID, status: 'CREATED', source_type: 'MANUAL_TOOL_TEST',
          trigger_type: 'TEST', mcp_tool_id: TOOL_ID,
          mcp_tool_version_id: validVersion.id, plan_hash: 'a'.repeat(64),
          requested_at: '2026-11-16T00:00:00Z', step_count: 1,
        }, 201));
      }
      return Promise.resolve(json({ error: { code: 'NOT_FOUND' } }, 404));
    });
    const user = userEvent.setup();
    view(fetchMock);
    await user.click(screen.getByRole('button', { name: '테스트 실행' }));
    await screen.findByText(/실행 작업 생성됨/);
    await user.click(screen.getByRole('button', { name: '실행 응답 확인 / 새로고침' }));
    expect(await screen.findByText('HTTP 200')).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(0);
  });
});
