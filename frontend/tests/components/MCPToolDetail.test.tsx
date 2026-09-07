import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPToolDetail from '@/screens/mcp/MCPToolDetail';
import { discoveredTool, invalidVersion, toolPolicy, validVersion, versionList, warningVersion } from '../fixtures/mcp-api';
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

  it('keeps concurrent Policy create 409 feedback after refetch shows latest policy', async () => {
    const user = userEvent.setup();
    let policyGets = 0;
    const latest = {
      ...toolPolicy,
      risk_class: 'IDEMPOTENT_WRITE',
      lock_version: 1,
      data_classification: 'from-other-create',
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.endsWith('/policy') && init?.method === 'PUT') {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'RESOURCE_CONFLICT',
                message: 'policy already exists',
                request_id: 'req-pol-create',
              },
            },
            409,
          ),
        );
      }
      if (url.endsWith('/policy')) {
        policyGets += 1;
        if (policyGets === 1) {
          return Promise.resolve(
            jsonResponse({ error: { code: 'NOT_FOUND', message: 'policy missing' } }, 404),
          );
        }
        return Promise.resolve(jsonResponse(latest));
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

    expect(await screen.findByText(/다른 작업에서 Policy가 생성되었습니다/i)).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-pol-create/i)).toBeInTheDocument();
    expect(await screen.findByText(/from-other-create/i)).toBeInTheDocument();
    expect(screen.getByText(/Idempotent Write/i)).toBeInTheDocument();
  });

  it('keeps stale Policy update 409 feedback after refetch', async () => {
    const user = userEvent.setup();
    let policyGets = 0;
    const newer = {
      ...toolPolicy,
      lock_version: 5,
      data_classification: 'refetched-latest',
      timeout_ms: 45000,
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.endsWith('/policy') && init?.method === 'PUT') {
        expect(init.headers).toEqual(expect.objectContaining({ 'If-Match': '1' }));
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'RESOURCE_VERSION_CONFLICT',
                message: 'lock mismatch',
                request_id: 'req-pol-stale',
              },
            },
            409,
          ),
        );
      }
      if (url.endsWith('/policy')) {
        policyGets += 1;
        return Promise.resolve(jsonResponse(policyGets === 1 ? toolPolicy : newer));
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
    expect(await screen.findByText(/Edit Policy/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Edit Policy/i }));
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    expect(await screen.findByText(/다른 작업으로 Policy가 변경되었습니다/i)).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-pol-stale/i)).toBeInTheDocument();
    expect(await screen.findByText(/refetched-latest/i)).toBeInTheDocument();
    expect(screen.getByText('45000')).toBeInTheDocument();
  });

  it('shows metadata PATCH stale conflict on detail alert after dialog closes', async () => {
    const user = userEvent.setup();
    let toolGets = 0;
    const latest = {
      ...discoveredTool,
      display_name: 'Latest From Peer',
      lock_version: 9,
      description_override: 'peer edit',
    };
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'PATCH') {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: 'RESOURCE_VERSION_CONFLICT',
                message: 'tool lock mismatch',
                request_id: 'req-meta-stale',
              },
            },
            409,
          ),
        );
      }
      if (url.match(/\/mcp\/tools\/[^/]+$/) && !url.includes('/versions')) {
        toolGets += 1;
        return Promise.resolve(jsonResponse(toolGets === 1 ? discoveredTool : latest));
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
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    expect(await screen.findByText(/다른 작업으로 Tool이 변경되었습니다/i)).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-meta-stale/i)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Latest From Peer' })).toBeInTheDocument();
    expect(screen.getByText('peer edit')).toBeInTheDocument();
  });

  it('blocks metadata PATCH when a tag exceeds 64 characters', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'PATCH') {
        throw new Error('PATCH must not be called');
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
    const tags = screen.getByPlaceholderText(/ops, search/i);
    fireEvent.change(tags, { target: { value: 'x'.repeat(65) } });
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    expect(await screen.findByText(/각 태그는 최대 64자까지 입력할 수 있습니다/i)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false);
    expect(screen.getByPlaceholderText(/ops, search/i)).toBeInTheDocument();
  });

  it('blocks metadata PATCH when more than 32 unique tags are provided', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'PATCH') {
        throw new Error('PATCH must not be called');
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
    const tags = screen.getByPlaceholderText(/ops, search/i);
    fireEvent.change(tags, {
      target: { value: Array.from({ length: 33 }, (_, i) => `t${i}`).join(',') },
    });
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    expect(await screen.findByText(/태그는 최대 32개까지 입력할 수 있습니다/i)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false);
  });

  it('normalizes blank/duplicate tags before PATCH', async () => {
    const user = userEvent.setup();
    const patched = { ...discoveredTool, tags: ['alpha', 'beta'], lock_version: 2 };
    let patchedBody: unknown;
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.url;
      if (init?.method === 'PATCH') {
        patchedBody = JSON.parse(String(init.body));
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
    const tags = screen.getByPlaceholderText(/ops, search/i);
    fireEvent.change(tags, { target: { value: ' alpha , , beta, alpha, beta ' } });
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    await waitFor(() => {
      expect(patchedBody).toEqual({
        display_name: 'Search Docs',
        description_override: null,
        tags: ['alpha', 'beta'],
      });
    });
    expect(await screen.findByText('alpha, beta')).toBeInTheDocument();
  });
});
