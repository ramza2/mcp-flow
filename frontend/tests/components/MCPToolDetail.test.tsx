import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPToolDetail from '@/screens/mcp/MCPToolDetail';
import { discoveredTool, invalidVersion, validVersion, versionList, warningVersion } from '../fixtures/mcp-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MCPToolDetail — API detail', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubToolFetch() {
    return vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions') && url.endsWith(warningVersion.id)) {
        return Promise.resolve(jsonResponse(warningVersion));
      }
      if (url.includes('/versions') && !url.includes(validVersion.id) && !url.includes(warningVersion.id)) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
  }

  it('loads tool overview and current version validation', async () => {
    vi.stubGlobal('fetch', stubToolFetch());

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    expect(await screen.findByRole('heading', { name: 'Search Docs' })).toBeInTheDocument();
    expect(screen.getAllByText('VALID').length).toBeGreaterThan(0);
    expect(screen.getAllByText('DISCOVERED').length).toBeGreaterThan(0);
  });

  it('shows 404 for missing tool', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ error: { code: 'NOT_FOUND', message: 'missing' } }, 404),
      ),
    );

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: '/mcp/tools/missing',
    });

    expect(await screen.findByText(/Tool을 찾을 수 없습니다/i)).toBeInTheDocument();
  });

  it('renders input schema via JsonViewer', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', stubToolFetch());

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: /^Input Schema$/i }));

    expect(await screen.findByText(/"query"/)).toBeInTheDocument();
  });

  it('lists versions and loads selected version schemas', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes('/versions') && url.endsWith(warningVersion.id)) {
        return Promise.resolve(jsonResponse(warningVersion));
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: /^Versions$/i }));
    await user.click(screen.getByText('v3'));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(warningVersion.id),
        expect.any(Object),
      );
    });
    expect(screen.getAllByText('WARNING').length).toBeGreaterThan(0);
  });

  it('ignores stale Version A response after Version B is selected', async () => {
    const user = userEvent.setup();
    let resolveA!: (value: Response) => void;
    const pendingA = new Promise<Response>(resolve => {
      resolveA = resolve;
    });
    let aSignal: AbortSignal | undefined;

    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes('/versions') && url.endsWith(warningVersion.id)) {
        aSignal = init?.signal ?? undefined;
        return pendingA;
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: /^Versions$/i }));

    // Version A (v3) — keep pending
    await user.click(screen.getByText('v3'));
    await waitFor(() => {
      expect(aSignal).toBeDefined();
    });

    // Version B (v1) — resolves first
    await user.click(screen.getByText('v1'));
    await waitFor(() => {
      expect(aSignal?.aborted).toBe(true);
    });

    // Late A response must not overwrite B
    resolveA(jsonResponse(warningVersion));

    await user.click(screen.getByRole('button', { name: /^Overview$/i }));
    expect(await screen.findByText('v1')).toBeInTheDocument();
    expect(screen.getAllByText('VALID').length).toBeGreaterThan(0);
    expect(screen.queryByText('v3')).not.toBeInTheDocument();
  });

  it('shows version-specific error when detail API fails without losing Tool Overview', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes('/versions') && url.endsWith(warningVersion.id)) {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'INTERNAL_ERROR',
                message: 'version detail failed',
                request_id: 'req-ver-500',
              },
            },
            500,
          ),
        );
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    expect(await screen.findByRole('heading', { name: 'Search Docs' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^Versions$/i }));
    await user.click(screen.getByText('v3'));

    expect(await screen.findByText(/version detail failed/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Search Docs' })).toBeInTheDocument();
    expect(screen.getByText('DISCOVERED')).toBeInTheDocument();
  });

  it('shows deferred Test Call / Used By / Audit as empty state', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', stubToolFetch());

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: /^Test Call$/i }));
    expect(await screen.findByText(/Test Call deferred/i)).toBeInTheDocument();
  });

  it('header Activate uses If-Match and updates status + lock_version', async () => {
    const user = userEvent.setup();
    const activated = { ...discoveredTool, status: 'ACTIVE' as const, lock_version: 2 };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/activate') && init?.method === 'POST') {
        expect(init.headers).toEqual(expect.objectContaining({ 'If-Match': '1' }));
        return Promise.resolve(jsonResponse(activated));
      }
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: 'Activate' }));
    expect(await screen.findByRole('button', { name: 'Deactivate' })).toBeInTheDocument();
    expect(screen.getAllByText('ACTIVE').length).toBeGreaterThan(0);
  });

  it('metadata edit PATCHes canonical body with If-Match', async () => {
    const user = userEvent.setup();
    const patched = {
      ...discoveredTool,
      display_name: 'Docs Search',
      tags: ['docs'],
      lock_version: 2,
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'PATCH') {
        expect(init.headers).toEqual(expect.objectContaining({ 'If-Match': '1' }));
        expect(JSON.parse(String(init.body))).toEqual({
          display_name: 'Docs Search',
          description_override: null,
          tags: ['docs'],
        });
        return Promise.resolve(jsonResponse(patched));
      }
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: /Edit metadata/i }));
    await user.clear(screen.getByDisplayValue('Search Docs'));
    await user.type(screen.getByRole('textbox', { name: /Display Name/i }), 'Docs Search');
    const tags = screen.getByPlaceholderText(/ops, search/i);
    await user.clear(tags);
    await user.type(tags, 'docs');
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    expect(await screen.findByRole('heading', { name: 'Docs Search' })).toBeInTheDocument();
  });

  it('Policy GET 404 shows empty create state; create PUT has no If-Match', async () => {
    const user = userEvent.setup();
    const created = {
      id: 'pol-new',
      mcp_tool_id: discoveredTool.id,
      risk_class: 'UNKNOWN',
      requires_confirmation: false,
      requires_approval: false,
      approval_policy_id: null,
      timeout_ms: 30000,
      max_attempts: 1,
      backoff_policy: null,
      max_result_bytes: 1048576,
      allow_auto_select: true,
      data_classification: null,
      policy_metadata: null,
      updated_at: '2026-09-07T00:00:00Z',
      updated_by: null,
      lock_version: 1,
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.endsWith('/policy') && init?.method === 'PUT') {
        expect((init.headers as Record<string, string>)?.['If-Match']).toBeUndefined();
        return Promise.resolve(jsonResponse(created));
      }
      if (url.endsWith('/policy')) {
        return Promise.resolve(
          jsonResponse({ error: { code: 'NOT_FOUND', message: 'policy missing' } }, 404),
        );
      }
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: /^Policy$/i }));
    expect(await screen.findByText(/아직 Tool Policy가 설정되지 않았습니다/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Create Policy/i }));
    await user.click(screen.getByRole('button', { name: /^Save$/i }));
    expect(await screen.findByText(/Edit Policy/i)).toBeInTheDocument();
  });

  it('Verification is version-scoped and ignores stale list for prior version', async () => {
    const user = userEvent.setup();
    let resolveV1!: (value: Response) => void;
    const pendingV1 = new Promise<Response>(resolve => {
      resolveV1 = resolve;
    });
    let v1Signal: AbortSignal | undefined;

    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(discoveredTool));
      }
      if (url.includes(`/versions/${validVersion.id}/verifications`)) {
        v1Signal = init?.signal ?? undefined;
        return pendingV1;
      }
      if (url.includes(`/versions/${warningVersion.id}/verifications`)) {
        return Promise.resolve(
          jsonResponse({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
        );
      }
      if (url.includes('/versions') && url.endsWith(warningVersion.id)) {
        return Promise.resolve(jsonResponse(warningVersion));
      }
      if (url.includes('/versions') && url.endsWith(validVersion.id)) {
        return Promise.resolve(jsonResponse(validVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse(versionList));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    await user.click(screen.getByRole('button', { name: /^Verification$/i }));
    await waitFor(() => expect(v1Signal).toBeDefined());

    await user.click(screen.getByRole('button', { name: /^Versions$/i }));
    await user.click(screen.getByText('v3'));
    await user.click(screen.getByRole('button', { name: /^Verification$/i }));

    await waitFor(() => expect(v1Signal?.aborted).toBe(true));
    resolveV1(
      jsonResponse({
        items: [
          {
            id: 'stale',
            mcp_tool_version_id: validVersion.id,
            status: 'PENDING',
            verified_by: null,
            verified_at: '2026-09-02T16:00:00Z',
            test_execution_id: null,
            criteria_version: 'stale',
            result_summary: null,
            evidence_blob_id: null,
            expires_at: null,
          },
        ],
        page: 1,
        page_size: 20,
        total: 1,
        has_next: false,
      }),
    );

    expect(await screen.findByText(/v3 · Historical/i)).toBeInTheDocument();
    expect(screen.queryByText('stale')).not.toBeInTheDocument();
  });

  it('shows INVALID validation separately from tool status', async () => {
    const tool = { ...discoveredTool, status: 'ACTIVE' as const, current_version_id: invalidVersion.id };
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        return Promise.resolve(jsonResponse(tool));
      }
      if (url.includes('/versions') && url.endsWith(invalidVersion.id)) {
        return Promise.resolve(jsonResponse(invalidVersion));
      }
      if (url.includes('/versions')) {
        return Promise.resolve(jsonResponse({ ...versionList, items: [invalidVersion] }));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<MCPToolDetail />, {
      path: '/mcp/tools/:toolId',
      route: `/mcp/tools/${discoveredTool.id}`,
    });

    await screen.findByRole('heading', { name: 'Search Docs' });
    expect(screen.getAllByText('ACTIVE').length).toBeGreaterThan(0);
    expect(screen.getAllByText('INVALID').length).toBeGreaterThan(0);
  });
});
