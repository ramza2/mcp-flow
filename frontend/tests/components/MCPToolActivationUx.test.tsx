import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import MCPTools from '@/screens/mcp/MCPTools';
import MCPToolDetail from '@/screens/mcp/MCPToolDetail';
import {
  buildMcpToolSetupSteps,
  deriveMcpToolSetupFlags,
  listEmptyGuidance,
  listLoadFailureGuidance,
  nextMcpToolSetupAction,
  toolLifecycleDisabledReason,
  toolStatusMeaningNote,
} from '@/screens/mcp/mcpToolSetup';
import {
  blockedTool,
  discoveredTool,
  missingTool,
  serverList,
  validVersion,
  versionList,
} from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

import { buildAuthContext } from '../fixtures/auth-context';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCP tool activation UX helpers', () => {
  it('builds find→status→policy→activate from existing tool fields', () => {
    const flags = deriveMcpToolSetupFlags({
      status: 'DISCOVERED',
      current_version_id: 'ver-1',
    });
    const steps = buildMcpToolSetupSteps(flags);
    expect(steps.map(s => [s.id, s.label, s.done])).toEqual([
      ['find', '도구 찾기', true],
      ['status', '상태 확인', true],
      ['policy', '실행 정책·권한', false],
      ['activate', '활성화', false],
    ]);
    expect(nextMcpToolSetupAction(steps)?.stepId).toBe('policy');
    expect(toolStatusMeaningNote()).toMatch(/VERIFIED/);
    expect(toolStatusMeaningNote()).toMatch(/DRAFT/);
  });

  it('keeps ACTIVE activate done without inferring policyReviewed', () => {
    const flags = deriveMcpToolSetupFlags({
      status: 'ACTIVE',
      current_version_id: 'ver-1',
    });
    expect(flags.activated).toBe(true);
    expect(flags.policyReviewed).toBe(false);
    const steps = buildMcpToolSetupSteps(flags);
    expect(steps.find(s => s.id === 'activate')?.done).toBe(true);
    expect(steps.find(s => s.id === 'policy')?.done).toBe(false);
    const next = nextMcpToolSetupAction(steps, {
      status: 'ACTIVE',
      current_version_id: 'ver-1',
    });
    expect(next?.stepId).toBe('policy');
    expect(next?.message).toMatch(/권장/);
    expect(next?.message).toMatch(/완료로 표시하지 않습니다/);
  });

  it('explains activate blocks for MISSING/BLOCKED/no version/permission', () => {
    expect(toolLifecycleDisabledReason(missingTool, { canEdit: true })).toMatch(/누락/);
    expect(toolLifecycleDisabledReason(blockedTool, { canEdit: true })).toMatch(/차단/);
    expect(toolLifecycleDisabledReason(
      { ...discoveredTool, current_version_id: null },
      { canEdit: true },
    )).toMatch(/현재 버전/);
    expect(toolLifecycleDisabledReason(discoveredTool, { canEdit: false })).toMatch(/mcp\.server\.manage/);
    expect(toolLifecycleDisabledReason(discoveredTool, { canEdit: true })).toBeNull();
  });

  it('guides empty and forbidden list failures in Korean', () => {
    expect(listEmptyGuidance({ hasFilters: false }).description).toMatch(/탐색 결과 적용/);
    expect(listEmptyGuidance({ hasFilters: true }).title).toMatch(/조건에 맞는/);
    expect(listLoadFailureGuidance('FORBIDDEN')).toMatch(/mcp\.tool\.read/);
  });
});

const manageAuth = () =>
  buildAuthContext({ permissions: ['mcp.tool.read', 'mcp.server.manage'] });

describe('MCPTools activation UX', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows setup flow and body reasons for blocked/missing tools', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/mcp/tools')) {
          return Promise.resolve(jsonResponse({
            items: [missingTool, blockedTool],
            page: 1,
            page_size: 20,
            total: 2,
            has_next: false,
          }));
        }
        if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    renderWithRouter(<MCPTools />, {
      path: '/mcp/tools',
      route: '/mcp/tools',
      authValue: manageAuth(),
    });
    expect(await screen.findByTestId('mcp-tool-setup-progress')).toHaveTextContent(/도구 찾기/);
    expect(screen.getByText(/VERIFIED/)).toBeInTheDocument();
    const reasons = await screen.findAllByTestId('mcp-tool-list-disabled-reason');
    expect(reasons.some(el => /누락/.test(el.textContent ?? ''))).toBe(true);
    expect(reasons.some(el => /차단/.test(el.textContent ?? ''))).toBe(true);
  });

  it('shows permission next action when tool list is forbidden', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/mcp/tools')) {
          return Promise.resolve(jsonResponse(
            { error: { code: 'FORBIDDEN', message: 'denied', request_id: 'req-deny' } },
            403,
          ));
        }
        if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    renderWithRouter(<MCPTools />, {
      path: '/mcp/tools',
      route: '/mcp/tools',
      authValue: manageAuth(),
    });
    expect(await screen.findByTestId('mcp-tool-list-error-next')).toHaveTextContent(/mcp\.tool\.read/);
  });

  it('disables activate actions when mcp.server.manage is missing (UX helper only)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.includes('/mcp/tools')) {
          return Promise.resolve(jsonResponse({
            items: [discoveredTool],
            page: 1,
            page_size: 20,
            total: 1,
            has_next: false,
          }));
        }
        if (url.includes('/mcp/servers')) return Promise.resolve(jsonResponse(serverList));
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    renderWithRouter(<MCPTools />, {
      path: '/mcp/tools',
      route: '/mcp/tools',
      authValue: buildAuthContext({ permissions: ['mcp.tool.read'] }),
    });
    expect(await screen.findByText('Search Docs')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '활성화' })).not.toBeInTheDocument();
    expect(screen.getByTestId('mcp-tool-list-disabled-reason')).toHaveTextContent(/mcp\.server\.manage/);
  });
});

describe('MCPToolDetail activation UX', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows next policy step and blocks activate without current version', async () => {
    const noVersion = { ...discoveredTool, current_version_id: null };
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
          return Promise.resolve(jsonResponse(noVersion));
        }
        if (url.includes('/versions')) {
          return Promise.resolve(jsonResponse({
            items: [],
            page: 1,
            page_size: 20,
            total: 0,
            has_next: false,
          }));
        }
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${noVersion.id}`,
      authValue: manageAuth(),
    });

    expect(await screen.findByTestId('mcp-tool-setup-progress')).toBeInTheDocument();
    expect(screen.getByTestId('mcp-tool-setup-step-policy')).toHaveAttribute('data-done', 'false');
    expect(screen.getByTestId('mcp-tool-setup-next')).toHaveTextContent(/현재 버전/);
    expect(screen.getByTestId('mcp-tool-activate-disabled-reason')).toHaveTextContent(/현재 버전/);
    expect(screen.getByRole('button', { name: '활성화' })).toBeDisabled();
  });

  it('marks activate complete when ACTIVE and clarifies VERIFIED is separate', async () => {
    const active = { ...discoveredTool, status: 'ACTIVE' as const };
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo) => {
        const url = typeof input === 'string' ? input : input.url;
        if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
          return Promise.resolve(jsonResponse(active));
        }
        if (url.includes('/versions') && url.endsWith(validVersion.id)) {
          return Promise.resolve(jsonResponse(validVersion));
        }
        if (url.includes('/versions')) return Promise.resolve(jsonResponse(versionList));
        return Promise.resolve(new Response('Not found', { status: 404 }));
      }),
    );

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${active.id}`,
      authValue: manageAuth(),
    });

    expect(await screen.findByTestId('mcp-tool-setup-step-activate')).toHaveAttribute('data-done', 'true');
    expect(screen.getByTestId('mcp-tool-setup-step-policy')).toHaveAttribute('data-done', 'false');
    await waitFor(() => {
      expect(screen.getByTestId('mcp-tool-setup-next')).toHaveTextContent(/권장/);
    });
    expect(screen.getByTestId('mcp-tool-setup-next')).toHaveTextContent(/VERIFIED/);
    expect(screen.getByText(/서버 초안\(DRAFT\)≠도구 상태/)).toBeInTheDocument();
  });
});
