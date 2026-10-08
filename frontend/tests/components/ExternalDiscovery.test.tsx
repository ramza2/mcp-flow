import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ExternalDiscovery from '@/screens/mcp/ExternalDiscovery';
import {
  importResponse,
  officialSource,
  packageOnlyCandidate,
  remoteCandidate,
  reviewResponse,
  searchFailed,
  searchSucceeded,
  sourceList,
} from '../fixtures/mcp-discovery-api';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function apiError(status: number, code: string, message: string, requestId = 'req-test') {
  return jsonResponse(
    { error: { code, message, request_id: requestId, details: [] } },
    status,
  );
}

function sourcesThen(handler: (url: string, init?: RequestInit) => Promise<Response> | Response) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (url.includes('/mcp-discovery/sources') && (!init?.method || init.method === 'GET')) {
      return Promise.resolve(jsonResponse(sourceList));
    }
    return Promise.resolve(handler(url, init));
  });
}

async function renderReady() {
  renderWithRouter(<ExternalDiscovery />, {
    path: '/mcp/discovery',
    route: '/mcp/discovery',
  });
  expect(await screen.findByDisplayValue('Official MCP Registry')).toBeInTheDocument();
}

describe('ExternalDiscovery — real API wiring', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('A. loads sources and selects enabled Official Registry', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(sourceList)));
    await renderReady();
    expect(screen.getByRole('option', { name: 'Official MCP Registry' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Disabled Registry/ })).toBeDisabled();
  });

  it('B. source load 500 shows ErrorState + request id; retry works', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(apiError(500, 'INTERNAL', 'source boom', 'req-src-500'))
      .mockResolvedValueOnce(jsonResponse(sourceList));
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<ExternalDiscovery />, {
      path: '/mcp/discovery',
      route: '/mcp/discovery',
    });

    expect(await screen.findByText('source boom')).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-src-500/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /다시 시도/i }));
    expect(await screen.findByDisplayValue('Official MCP Registry')).toBeInTheDocument();
  });

  it('C. read 403 AUTH_FORBIDDEN shows PermissionDenied', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(apiError(403, 'AUTH_FORBIDDEN', 'Missing mcp.server.read')),
    );
    renderWithRouter(<ExternalDiscovery />, {
      path: '/mcp/discovery',
      route: '/mcp/discovery',
    });
    expect(await screen.findByText('접근 권한이 없습니다')).toBeInTheDocument();
  });

  it('D. search success posts correct body and renders API candidates (no mock)', async () => {
    const fetchMock = sourcesThen((url, init) => {
      if (url.includes('/mcp-discovery/searches') && init?.method === 'POST') {
        return jsonResponse(searchSucceeded([remoteCandidate]));
      }
      return apiError(404, 'NOT_FOUND', `unexpected ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), '  slack  ');
    await user.click(screen.getByRole('button', { name: '검색' }));

    expect(await screen.findByText('Slack MCP')).toBeInTheDocument();
    expect(screen.getByText(/1개 Candidate 검색됨/)).toBeInTheDocument();
    expect(screen.getByText(/Slack integration over Streamable HTTP/)).toBeInTheDocument();
    expect(screen.getByText(/Source: Official MCP Registry/)).toBeInTheDocument();
    // Mock trust states / registry labels must not appear.
    expect(screen.queryByText('UNDER_REVIEW')).not.toBeInTheDocument();
    expect(screen.queryByText('mcp.run')).not.toBeInTheDocument();

    const postCall = fetchMock.mock.calls.find(
      (c) => String(c[0]).includes('/searches') && c[1]?.method === 'POST',
    );
    expect(postCall).toBeDefined();
    const postInit = postCall?.[1];
    expect(postInit).toBeDefined();
    expect(JSON.parse(String(postInit?.body))).toEqual({
      source_id: officialSource.id,
      q: 'slack',
      limit: 20,
    });
  });

  it('E. durable FAILED search (HTTP 200) shows error_code/message, not candidates', async () => {
    vi.stubGlobal(
      'fetch',
      sourcesThen((url, init) => {
        if (url.includes('/searches') && init?.method === 'POST') {
          return jsonResponse(searchFailed);
        }
        return apiError(404, 'NOT_FOUND', 'unexpected');
      }),
    );

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'boom');
    await user.click(screen.getByRole('button', { name: '검색' }));

    expect(
      await screen.findByText('Official Registry temporarily unavailable'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Slack MCP')).not.toBeInTheDocument();
  });

  it('F. empty search result shows empty state', async () => {
    vi.stubGlobal(
      'fetch',
      sourcesThen((url, init) => {
        if (url.includes('/searches') && init?.method === 'POST') {
          return jsonResponse(searchSucceeded([]));
        }
        return apiError(404, 'NOT_FOUND', 'unexpected');
      }),
    );

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'zzz');
    await user.click(screen.getByRole('button', { name: '검색' }));

    expect(await screen.findByText('검색 결과가 없습니다')).toBeInTheDocument();
  });

  it('G. APPROVE updates review_state without new search', async () => {
    const fetchMock = sourcesThen((url, init) => {
      if (url.includes('/searches') && init?.method === 'POST') {
        return jsonResponse(searchSucceeded([remoteCandidate]));
      }
      if (url.includes('/reviews') && init?.method === 'POST') {
        return jsonResponse(reviewResponse(remoteCandidate.id, 'APPROVE', 'APPROVED'));
      }
      return apiError(404, 'NOT_FOUND', `unexpected ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'slack');
    await user.click(screen.getByRole('button', { name: '검색' }));
    await screen.findByText('Slack MCP');

    await user.click(screen.getByRole('button', { name: '승인' }));
    expect(await screen.findByRole('heading', { name: '후보 승인' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '승인 제출' }));

    await waitFor(() => {
      expect(screen.getByText('Approved')).toBeInTheDocument();
    });
    const searchPosts = fetchMock.mock.calls.filter(
      (c) => String(c[0]).includes('/searches') && c[1]?.method === 'POST',
    );
    expect(searchPosts).toHaveLength(1);
  });

  it('H. REJECT then re-APPROVE updates latest effective state', async () => {
    let reviewState: 'UNREVIEWED' | 'APPROVED' | 'REJECTED' = 'UNREVIEWED';
    const fetchMock = sourcesThen((url, init) => {
      if (url.includes('/searches') && init?.method === 'POST') {
        return jsonResponse(
          searchSucceeded([{ ...remoteCandidate, review_state: reviewState }]),
        );
      }
      if (url.includes('/reviews') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as { decision: 'APPROVE' | 'REJECT' };
        if (body.decision === 'REJECT') {
          reviewState = 'REJECTED';
          return jsonResponse(reviewResponse(remoteCandidate.id, 'REJECT', 'REJECTED'));
        }
        reviewState = 'APPROVED';
        return jsonResponse(reviewResponse(remoteCandidate.id, 'APPROVE', 'APPROVED'));
      }
      return apiError(404, 'NOT_FOUND', `unexpected ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'slack');
    await user.click(screen.getByRole('button', { name: '검색' }));
    await screen.findByText('Slack MCP');

    await user.click(screen.getByRole('button', { name: '거절' }));
    await user.click(await screen.findByRole('button', { name: '거절 제출' }));
    await waitFor(() => expect(screen.getByText('Rejected')).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: '승인' }));
    await user.click(await screen.findByRole('button', { name: '승인 제출' }));
    await waitFor(() => expect(screen.getByText('Approved')).toBeInTheDocument());
  });

  it('I. import gating: UNREVIEWED/REJECTED disabled; APPROVED+remote enabled', async () => {
    const fetchMock = sourcesThen((url, init) => {
      if (url.includes('/searches') && init?.method === 'POST') {
        return jsonResponse(searchSucceeded([remoteCandidate]));
      }
      if (url.includes('/reviews') && init?.method === 'POST') {
        return jsonResponse(reviewResponse(remoteCandidate.id, 'APPROVE', 'APPROVED'));
      }
      return apiError(404, 'NOT_FOUND', `unexpected ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'slack');
    await user.click(screen.getByRole('button', { name: '검색' }));
    await screen.findByText('Slack MCP');

    const card = screen.getByTestId(`candidate-${remoteCandidate.id}`);
    expect(within(card).getByRole('button', { name: 'Import' })).toBeDisabled();
    expect(
      within(card).getByText(/Import하려면 최신 검토 결과가 APPROVE여야 합니다/),
    ).toBeInTheDocument();

    await user.click(within(card).getByRole('button', { name: '승인' }));
    await user.click(await screen.findByRole('button', { name: '승인 제출' }));
    await waitFor(() => {
      expect(within(card).getByRole('button', { name: 'Import' })).toBeEnabled();
    });
  });

  it('J. package/local-only candidate visible; Import disabled; no fabricated endpoint', async () => {
    vi.stubGlobal(
      'fetch',
      sourcesThen((url, init) => {
        if (url.includes('/searches') && init?.method === 'POST') {
          return jsonResponse(searchSucceeded([packageOnlyCandidate]));
        }
        return apiError(404, 'NOT_FOUND', 'unexpected');
      }),
    );

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'local');
    await user.click(screen.getByRole('button', { name: '검색' }));

    expect(await screen.findByText('Local Package MCP')).toBeInTheDocument();
    expect(screen.getByText(/Remote endpoint 없음/)).toBeInTheDocument();
    expect(
      screen.getByText('원격 MCP endpoint가 없어 현재 Import할 수 없습니다.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/mcp\.example\.com/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import' })).toBeDisabled();
  });

  it('K. import success created=true shows DRAFT and Draft Server link', async () => {
    const approved = { ...remoteCandidate, review_state: 'APPROVED' as const };
    const fetchMock = sourcesThen((url, init) => {
      if (url.includes('/searches') && init?.method === 'POST') {
        return jsonResponse(searchSucceeded([approved]));
      }
      if (url.includes('/import') && init?.method === 'POST') {
        return jsonResponse(importResponse(approved.id, { created: true }));
      }
      return apiError(404, 'NOT_FOUND', `unexpected ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'slack');
    await user.click(screen.getByRole('button', { name: '검색' }));
    await screen.findByText('Slack MCP');

    await user.click(screen.getByRole('button', { name: 'Import' }));
    expect(await screen.findByText('DRAFT 생성 완료')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Draft Server 보기' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /srv-draft-imported-001/ })).toHaveAttribute(
      'href',
      '/mcp/servers/srv-draft-imported-001',
    );
  });

  it('L. idempotent import created=false is success', async () => {
    const approved = { ...remoteCandidate, review_state: 'APPROVED' as const };
    vi.stubGlobal(
      'fetch',
      sourcesThen((url, init) => {
        if (url.includes('/searches') && init?.method === 'POST') {
          return jsonResponse(searchSucceeded([approved]));
        }
        if (url.includes('/import') && init?.method === 'POST') {
          return jsonResponse(
            importResponse(approved.id, {
              created: false,
              mcpServerId: 'srv-existing-draft',
            }),
          );
        }
        return apiError(404, 'NOT_FOUND', 'unexpected');
      }),
    );

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'slack');
    await user.click(screen.getByRole('button', { name: '검색' }));
    await user.click(await screen.findByRole('button', { name: 'Import' }));

    expect(await screen.findByText('DRAFT 생성 완료')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /srv-existing-draft/ })).toBeInTheDocument();
  });

  it('M. import failure shows safe error and does not mark imported', async () => {
    const approved = { ...remoteCandidate, review_state: 'APPROVED' as const };
    vi.stubGlobal(
      'fetch',
      sourcesThen((url, init) => {
        if (url.includes('/searches') && init?.method === 'POST') {
          return jsonResponse(searchSucceeded([approved]));
        }
        if (url.includes('/import') && init?.method === 'POST') {
          return apiError(422, 'VALIDATION_ERROR', 'import blocked');
        }
        return apiError(404, 'NOT_FOUND', 'unexpected');
      }),
    );

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'slack');
    await user.click(screen.getByRole('button', { name: '검색' }));
    await user.click(await screen.findByRole('button', { name: 'Import' }));

    expect(await screen.findByText('import blocked')).toBeInTheDocument();
    expect(screen.queryByText('DRAFT 생성 완료')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import' })).toBeInTheDocument();
  });

  it('N. unsafe repository/homepage schemes are not clickable links', async () => {
    vi.stubGlobal(
      'fetch',
      sourcesThen((url, init) => {
        if (url.includes('/searches') && init?.method === 'POST') {
          return jsonResponse(searchSucceeded([packageOnlyCandidate]));
        }
        return apiError(404, 'NOT_FOUND', 'unexpected');
      }),
    );

    const user = userEvent.setup();
    await renderReady();
    await user.type(screen.getByPlaceholderText(/Slack/i), 'local');
    await user.click(screen.getByRole('button', { name: '검색' }));
    await screen.findByText('Local Package MCP');

    expect(screen.queryByRole('link', { name: /Repository/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Homepage/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Repository: \(unsafe URL\)/)).toBeInTheDocument();
    expect(screen.getByText(/Homepage: \(unsafe URL\)/)).toBeInTheDocument();
  });

  it('O. stale search response cannot overwrite newer results', async () => {
    let resolveSlow: ((value: Response) => void) | null = null;
    const slowPromise = new Promise<Response>((resolve) => {
      resolveSlow = resolve;
    });
    const searchBodies: string[] = [];

    const fetchMock = sourcesThen((url, init) => {
      if (url.includes('/searches') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as { q: string };
        searchBodies.push(body.q);
        if (body.q === 'slow') {
          return slowPromise;
        }
        return jsonResponse(
          searchSucceeded([{ ...remoteCandidate, name: 'Fast Result MCP', id: 'cand-fast' }]),
        );
      }
      return apiError(404, 'NOT_FOUND', 'unexpected');
    });
    vi.stubGlobal('fetch', fetchMock);

    const user = userEvent.setup();
    await renderReady();

    const input = screen.getByPlaceholderText(/Slack/i);
    await user.clear(input);
    await user.type(input, 'slow');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(searchBodies).toContain('slow'));

    await user.clear(input);
    await user.type(input, 'fast');
    await user.keyboard('{Enter}');

    expect(await screen.findByText('Fast Result MCP')).toBeInTheDocument();
    expect(searchBodies).toEqual(['slow', 'fast']);

    resolveSlow!(
      jsonResponse(
        searchSucceeded([{ ...remoteCandidate, name: 'Stale Result MCP', id: 'cand-stale' }]),
      ),
    );

    await waitFor(() => {
      expect(screen.getByText('Fast Result MCP')).toBeInTheDocument();
    });
    expect(screen.queryByText('Stale Result MCP')).not.toBeInTheDocument();
  });
});
