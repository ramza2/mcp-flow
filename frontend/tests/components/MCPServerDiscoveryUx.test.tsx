import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPServerNew from '@/screens/mcp/MCPServerNew';
import MCPServerDetail from '@/screens/mcp/MCPServerDetail';
import {
  applyDiscoveryDisabledReason,
  buildMcpServerSetupSteps,
  deriveMcpServerSetupFlags,
  labelFactoryReconcileStatus,
  nextMcpServerSetupAction,
  registerSubmitDisabledReason,
  registerWizardNextDisabledReason,
} from '@/screens/mcp/mcpServerSetup';
import {
  activeServer,
  draftServer,
  previewDiscovery,
  succeededCheck,
} from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCP server discovery UX helpers', () => {
  it('builds register→connect→discover→apply from existing fields only', () => {
    const flags = deriveMcpServerSetupFlags({
      lastHealthyAt: null,
      lastTest: null,
      previewResult: null,
      discoveries: [],
      toolsTotal: 0,
    });
    const steps = buildMcpServerSetupSteps({ registered: true, ...flags });
    expect(steps.map(s => [s.id, s.label, s.done])).toEqual([
      ['register', '등록', true],
      ['connect', '연결 확인', false],
      ['discover', '도구 탐색', false],
      ['apply', '결과 적용', false],
    ]);
    expect(nextMcpServerSetupAction(steps)?.stepId).toBe('connect');
  });

  it('treats recent connection failure as not connected even with past healthy history', () => {
    const failedRecent = deriveMcpServerSetupFlags({
      lastHealthyAt: '2026-09-01T10:00:00Z',
      lastTest: { status: 'FAILED' },
      previewResult: null,
      discoveries: [],
      toolsTotal: 0,
    });
    expect(failedRecent.connectionOk).toBe(false);
    expect(nextMcpServerSetupAction(
      buildMcpServerSetupSteps({ registered: true, ...failedRecent }),
    )?.stepId).toBe('connect');

    const timedOutRecent = deriveMcpServerSetupFlags({
      lastHealthyAt: '2026-09-01T10:00:00Z',
      lastTest: { status: 'TIMED_OUT' },
      previewResult: null,
      discoveries: [],
      toolsTotal: 0,
    });
    expect(timedOutRecent.connectionOk).toBe(false);

    const healthyFallback = deriveMcpServerSetupFlags({
      lastHealthyAt: '2026-09-01T10:00:00Z',
      lastTest: null,
      previewResult: null,
      discoveries: [],
      toolsTotal: 0,
    });
    expect(healthyFallback.connectionOk).toBe(true);

    const recentSuccess = deriveMcpServerSetupFlags({
      lastHealthyAt: null,
      lastTest: { status: 'SUCCEEDED' },
      previewResult: null,
      discoveries: [],
      toolsTotal: 0,
    });
    expect(recentSuccess.connectionOk).toBe(true);
  });

  it('marks apply done when tools exist and explains disabled apply in Korean', () => {
    const flags = deriveMcpServerSetupFlags({
      lastHealthyAt: '2026-09-02T14:20:00Z',
      lastTest: { status: 'SUCCEEDED' },
      previewResult: { success: true },
      discoveries: [{ success: true, apply_changes: true }],
      toolsTotal: 2,
    });
    const steps = buildMcpServerSetupSteps({ registered: true, ...flags });
    expect(steps.every(s => s.done)).toBe(true);
    expect(nextMcpServerSetupAction(steps)).toBeNull();

    expect(applyDiscoveryDisabledReason({
      previewResult: null,
      discovering: false,
      applying: false,
    })).toMatch(/미리보기/);
    expect(applyDiscoveryDisabledReason({
      previewResult: { success: false, factory_diff: null, error_message: 'boom' },
      discovering: false,
      applying: false,
    })).toMatch(/실패/);
    expect(applyDiscoveryDisabledReason({
      previewResult: {
        success: true,
        factory_diff: {
          status: 'MISMATCHED',
          matched: 0,
          missing_expected: 1,
          unexpected_remote: 0,
          contract_changed: 0,
          classifications: [],
          error_message: null,
        },
        error_message: null,
      },
      discovering: false,
      applying: false,
    })).toMatch(/정합성/);
    expect(labelFactoryReconcileStatus('CLEAN')).toBe('계약 일치');
    expect(labelFactoryReconcileStatus('AMBIGUOUS')).toBe('판정 불명확');
  });

  it('explains register wizard disables without inventing Domain states', () => {
    expect(registerWizardNextDisabledReason(0, '', '')).toMatch(/서버 이름/);
    expect(registerWizardNextDisabledReason(1, 'A', '')).toMatch(/서버 주소/);
    expect(registerSubmitDisabledReason('', '')).toMatch(/이름과/);
  });
});

describe('MCPServerNew beginner setup guidance', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows overall setup progress and disabled next reason in body text', async () => {
    const user = userEvent.setup();
    const view = renderWithRouter(<MCPServerNew />, {
      path: '/mcp/servers/new',
      route: '/mcp/servers/new',
    });

    expect(screen.getByTestId('mcp-server-setup-progress')).toBeInTheDocument();
    expect(screen.getByTestId('mcp-server-setup-step-register')).toHaveAttribute('data-done', 'false');
    expect(screen.getByTestId('mcp-server-setup-next')).toHaveTextContent(/등록/);
    expect(screen.getByTestId('mcp-register-next-disabled-reason')).toHaveTextContent(/서버 이름/);

    await user.type(screen.getByRole('textbox', { name: '서버 이름' }), 'Weather');
    expect(screen.queryByTestId('mcp-register-next-disabled-reason')).not.toBeInTheDocument();

    await user.click(within(view.container).getByRole('button', { name: /^다음$/ }));
    expect(screen.getByTestId('mcp-register-next-disabled-reason')).toHaveTextContent(/서버 주소/);
  });
});

describe('MCPServerDetail beginner setup and empty guidance', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows connect next action and apply disabled reason for draft without tools', async () => {
    const emptyTools = { items: [], page: 1, page_size: 20, total: 0, has_next: false };
    const emptyDiscoveries = { items: [], page: 1, page_size: 20, total: 0, has_next: false };
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/tools')) return Promise.resolve(jsonResponse(emptyTools));
        if (url.includes('/discoveries')) return Promise.resolve(jsonResponse(emptyDiscoveries));
        if (url.includes(`/mcp/servers/${draftServer.id}`)) {
          return Promise.resolve(jsonResponse(draftServer));
        }
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    const user = userEvent.setup();
    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${draftServer.id}`,
    });

    expect(await screen.findByRole('heading', { name: 'Weather MCP' })).toBeInTheDocument();
    expect(screen.getByTestId('mcp-server-setup-step-register')).toHaveAttribute('data-done', 'true');
    expect(screen.getByTestId('mcp-server-setup-step-connect')).toHaveAttribute('data-done', 'false');
    expect(screen.getByTestId('mcp-server-setup-next')).toHaveTextContent(/연결 테스트/);
    expect(screen.getByTestId('mcp-apply-disabled-reason')).toHaveTextContent(/미리보기/);

    await user.click(screen.getByRole('button', { name: /^도구\s*\d*$/i }));
    expect(await screen.findByText('이 서버에 등록된 도구가 없습니다')).toBeInTheDocument();
    expect(screen.getByText(/연결 테스트 → 도구 탐색 미리보기 → 탐색 결과 적용/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /탐색 이력/i }));
    expect(await screen.findByText('도구 탐색 이력이 없습니다')).toBeInTheDocument();
    expect(screen.getByText(/상단의 '도구 탐색 미리보기'를 실행하면/)).toBeInTheDocument();
  });

  it('shows Korean next action after connection success without auto-apply', async () => {
    let serverGets = 0;
    const emptyTools = { items: [], page: 1, page_size: 20, total: 0, has_next: false };
    const emptyDiscoveries = { items: [], page: 1, page_size: 20, total: 0, has_next: false };
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.url;
        if (init?.method === 'POST' && url.includes('/connection-tests')) {
          return Promise.resolve(jsonResponse(succeededCheck, 201));
        }
        if (init?.method === 'POST' && url.includes('/discoveries')) {
          return Promise.resolve(jsonResponse(previewDiscovery, 201));
        }
        if (url.includes('/tools')) return Promise.resolve(jsonResponse(emptyTools));
        if (url.includes('/discoveries')) return Promise.resolve(jsonResponse(emptyDiscoveries));
        if (url.includes(`/mcp/servers/${draftServer.id}`)) {
          serverGets += 1;
          return Promise.resolve(jsonResponse(
            serverGets === 1
              ? draftServer
              : { ...draftServer, last_healthy_at: '2026-09-04T08:00:00Z' },
          ));
        }
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    const user = userEvent.setup();
    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${draftServer.id}`,
    });

    await screen.findByText('Weather MCP');
    await user.click(screen.getByRole('button', { name: /연결 테스트/i }));
    expect(await screen.findByText(/연결 성공/i)).toBeInTheDocument();
    expect(screen.getAllByText(/다음: 도구 탐색 미리보기/).length).toBeGreaterThanOrEqual(1);

    await waitFor(() => {
      expect(screen.getByTestId('mcp-server-setup-step-connect')).toHaveAttribute('data-done', 'true');
    });

    await user.click(screen.getByRole('button', { name: /도구 탐색 미리보기/i }));
    expect(await screen.findByText(/도구 탐색 미리보기 — 성공/i)).toBeInTheDocument();
    expect(screen.getByText(/다음: 변경 내용을 확인한 뒤 탐색 결과 적용/)).toBeInTheDocument();
    expect(screen.queryByText(/자동 적용·자동 활성화/)).toBeNull();
  });

  it('keeps ACTIVE server apply blocked until preview exists', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/tools')) {
          return Promise.resolve(jsonResponse({
            items: [],
            page: 1,
            page_size: 20,
            total: 0,
            has_next: false,
          }));
        }
        if (url.includes('/discoveries')) {
          return Promise.resolve(jsonResponse({
            items: [],
            page: 1,
            page_size: 20,
            total: 0,
            has_next: false,
          }));
        }
        if (url.includes(`/mcp/servers/${activeServer.id}`)) {
          return Promise.resolve(jsonResponse({ ...activeServer, last_healthy_at: null }));
        }
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    renderWithRouter(<MCPServerDetail />, {
      path: '/mcp/servers/:serverId',
      route: `/mcp/servers/${activeServer.id}`,
    });

    expect(await screen.findByRole('heading', { name: 'Docs MCP' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '탐색 결과 적용' })).toBeDisabled();
    expect(screen.getByTestId('mcp-apply-disabled-reason')).toHaveTextContent(/미리보기/);
  });
});
