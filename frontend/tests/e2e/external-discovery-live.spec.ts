/**
 * E2E-013 live External MCP Discovery harness (docs/09).
 *
 * Real path: Chromium → HTTPS/Traefik → MCPFlow API → Official MCP Registry.
 * Skips unless PLAYWRIGHT_BASE_URL + credentials + discovery query envs are set.
 * Never mock/route-fulfill discovery APIs. Never log credentials, cookies, or auth headers.
 *
 * Mutation (APPROVE / Import) is intentionally NOT automated here — see
 * docs/pilot/external-discovery-live.md one-time operator procedure.
 */

import { expect, test, type Page, type Response } from '@playwright/test';

type LiveEnv = {
  username: string;
  password: string;
  query: string;
};

function readLiveEnv(): LiveEnv | null {
  const base = process.env.PLAYWRIGHT_BASE_URL?.trim();
  const username = process.env.MCPFLOW_E2E_USERNAME?.trim();
  const password = process.env.MCPFLOW_E2E_PASSWORD?.trim();
  const query = process.env.MCPFLOW_E2E_DISCOVERY_QUERY?.trim();
  if (!base || !username || !password || !query) {
    return null;
  }
  return { username, password, query };
}

const liveEnv = readLiveEnv();

type SafeNetworkHit = {
  method: string;
  pathname: string;
  status: number;
};

type SafeSearchSummary = {
  status: string;
  candidateCount: number;
  errorCode: string | null;
  errorMessage: string | null;
};

function pathnameOf(url: string): string | null {
  try {
    return new URL(url).pathname;
  } catch {
    return null;
  }
}

function isSearchPost(url: string, method: string): boolean {
  return method === 'POST' && pathnameOf(url) === '/api/v1/mcp-discovery/searches';
}

async function realLogin(page: Page, env: LiveEnv): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(/사용자 아이디/i).fill(env.username);
  await page.getByLabel(/비밀번호/i).fill(env.password);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });
}

async function summarizeSearchResponse(res: Response): Promise<SafeSearchSummary> {
  const body = (await res.json()) as {
    status?: string;
    candidate_count?: number;
    error_code?: string | null;
    error_message?: string | null;
  };
  return {
    status: String(body.status ?? ''),
    candidateCount: Number(body.candidate_count ?? 0),
    errorCode: body.error_code ?? null,
    errorMessage: body.error_message ?? null,
  };
}

function attachDiscoveryObservers(
  page: Page,
  hits: SafeNetworkHit[],
  onSearchResponse: (summary: SafeSearchSummary, status: number) => void,
): void {
  page.on('response', (res) => {
    const method = res.request().method().toUpperCase();
    const pathname = pathnameOf(res.url());
    if (!pathname?.startsWith('/api/v1/mcp-discovery')) return;

    // Safe metadata only — never dump headers/cookies/bodies here.
    hits.push({
      method,
      pathname,
      status: res.status(),
    });

    if (isSearchPost(res.url(), method)) {
      void summarizeSearchResponse(res)
        .then((summary) => onSearchResponse(summary, res.status()))
        .catch(() => {
          onSearchResponse(
            {
              status: 'PARSE_ERROR',
              candidateCount: 0,
              errorCode: 'NON_JSON_RESPONSE',
              errorMessage: 'search response was not JSON',
            },
            res.status(),
          );
        });
    }
  });
}

function assertNoSensitiveCandidateLeak(pageText: string): void {
  // Assert structured UI / raw-payload leaks — not free-text description words
  // that a Registry server might include in its public description.
  const forbidden = [
    /Install command/i,
    /Environment variables?/i,
    /Authorization\s*:/i,
    /Bearer\s+[A-Za-z0-9._\-]+/,
    /client_secret/i,
    /MCP_API_KEY/,
    /"inputSchema"\s*:/,
    /"packages"\s*:\s*\[/,
    /"remotes"\s*:\s*\[/,
    /"env"\s*:\s*\{/,
    /"headers"\s*:\s*\{/,
  ];
  for (const pattern of forbidden) {
    expect(pageText, `candidate UI must not expose ${pattern}`).not.toMatch(pattern);
  }
}

// Real credentials must not land in Playwright traces/videos/screenshots.
test.use({
  trace: 'off',
  video: 'off',
  screenshot: 'off',
});

test.describe('E2E-013 live External MCP Discovery', () => {
  test.describe.configure({ mode: 'serial', timeout: 120_000 });

  test.beforeEach(() => {
    test.skip(
      liveEnv == null,
      'Live discovery harness requires PLAYWRIGHT_BASE_URL, MCPFLOW_E2E_USERNAME, MCPFLOW_E2E_PASSWORD, MCPFLOW_E2E_DISCOVERY_QUERY',
    );
  });

  test('real Official Registry search renders candidates (read-only)', async ({
    page,
  }) => {
    const env = liveEnv!;
    const started = Date.now();
    const hits: SafeNetworkHit[] = [];
    let searchSummary: SafeSearchSummary | null = null;
    let searchHttpStatus: number | null = null;

    attachDiscoveryObservers(page, hits, (summary, status) => {
      searchSummary = summary;
      searchHttpStatus = status;
    });

    await realLogin(page, env);
    await page.goto('/mcp/discovery');

    await expect(
      page.getByRole('heading', { name: 'External MCP Discovery' }),
    ).toBeVisible({ timeout: 30_000 });

    const sourceSelect = page.locator('#discovery-source');
    const permissionDenied = page.getByText('접근 권한이 없습니다');
    const sourceLoadError = page.getByText('데이터를 불러오지 못했습니다');

    // Fail fast with concise diagnostics before waiting on the source select.
    await Promise.race([
      sourceSelect.waitFor({ state: 'visible', timeout: 30_000 }),
      permissionDenied.waitFor({ state: 'visible', timeout: 30_000 }).then(() => {
        throw new Error(
          'External Discovery PermissionDenied: E2E user needs mcp.server.read and mcp.server.manage',
        );
      }),
      sourceLoadError.waitFor({ state: 'visible', timeout: 30_000 }).then(() => {
        throw new Error(
          'External Discovery source load failed before source select appeared',
        );
      }),
    ]);

    await expect(sourceSelect).toContainText('Official MCP Registry');
    const selectedOption = sourceSelect.locator('option:checked');
    await expect(selectedOption).toHaveText(/Official MCP Registry/);
    await expect(selectedOption).toBeEnabled();

    await expect
      .poll(
        () => hits.some((h) => h.method === 'GET' && h.pathname === '/api/v1/mcp-discovery/sources' && h.status === 200),
        { timeout: 30_000 },
      )
      .toBe(true);

    const input = page.getByPlaceholder(/Slack, Notion, GitHub/i);
    await input.fill(env.query);
    await page.getByRole('button', { name: '검색' }).click();

    await expect
      .poll(
        () =>
          hits.some(
            (h) =>
              h.method === 'POST'
              && h.pathname === '/api/v1/mcp-discovery/searches'
              && h.status === 200,
          ),
        { timeout: 60_000 },
      )
      .toBe(true);

    await expect
      .poll(() => searchSummary != null, { timeout: 60_000 })
      .toBe(true);

    expect(searchHttpStatus, 'POST /searches must return HTTP 200').toBe(200);
    expect(
      searchSummary!.status,
      'current provider contract is synchronous terminal search',
    ).not.toBe('RUNNING');

    if (searchSummary!.status === 'FAILED') {
      const code = searchSummary!.errorCode ?? '(none)';
      const message = searchSummary!.errorMessage ?? '(none)';
      throw new Error(
        `Live discovery search FAILED error_code=${code} error_message=${message}`,
      );
    }

    expect(searchSummary!.status).toBe('SUCCEEDED');
    expect(searchSummary!.candidateCount).toBeGreaterThan(0);

    await expect(page.getByText(/\d+개 Candidate 검색됨/)).toBeVisible({
      timeout: 30_000,
    });

    const candidateCards = page.locator('[data-testid^="candidate-"]');
    await expect(candidateCards.first()).toBeVisible({ timeout: 30_000 });
    const cardCount = await candidateCards.count();
    expect(cardCount).toBeGreaterThan(0);

    const firstCard = candidateCards.first();
    await expect(firstCard.locator('h3').first()).not.toBeEmpty();
    await expect(
      firstCard.getByText(/Unreviewed|Approved|Rejected/i).first(),
    ).toBeVisible();
    await expect(firstCard.getByText(/Source:\s*Official MCP Registry/)).toBeVisible();
    await expect(
      firstCard.getByText(/Transport:\s*(STREAMABLE_HTTP|LEGACY_HTTP_SSE|STDIO|Remote endpoint 없음)/),
    ).toBeVisible();

    // No fatal application error surfaces after a successful search.
    await expect(page.getByText('데이터를 불러오지 못했습니다')).toHaveCount(0);
    await expect(page.getByText('접근 권한이 없습니다')).toHaveCount(0);

    const pageText = await page.getByRole('main').innerText();
    assertNoSensitiveCandidateLeak(pageText);

    // Do not follow external repository/homepage links.
    const externalLinks = firstCard.locator('a[target="_blank"]');
    const linkCount = await externalLinks.count();
    for (let i = 0; i < linkCount; i += 1) {
      const href = await externalLinks.nth(i).getAttribute('href');
      expect(href).toMatch(/^https?:\/\//i);
    }

    const sourcesHit = hits.find(
      (h) =>
        h.method === 'GET'
        && h.pathname === '/api/v1/mcp-discovery/sources'
        && h.status === 200,
    );
    const searchHit = hits.find(
      (h) =>
        h.method === 'POST'
        && h.pathname === '/api/v1/mcp-discovery/searches'
        && h.status === 200,
    );

    // Concise safe final report — no raw candidate payloads / credentials.
    // eslint-disable-next-line no-console
    console.log(
      [
        'External Discovery live PASS',
        'source=Official MCP Registry',
        `query=${env.query}`,
        `source_http=${sourcesHit?.status ?? 'missing'}`,
        `search_http=${searchHit?.status ?? 'missing'}`,
        `search_status=${searchSummary!.status}`,
        `candidate_count=${searchSummary!.candidateCount}`,
        `duration_ms=${Date.now() - started}`,
      ].join('\n'),
    );
  });
});
