/**
 * E2E-010 live Traefik/browser SSE harness (docs/09 §19/§20).
 *
 * Real path: Chromium EventSource → HTTPS → Traefik → api → PostgreSQL.
 * Skips unless PLAYWRIGHT_BASE_URL + credentials + execution fixture envs are set.
 * Never mock/route-fulfill SSE. Never log credentials, cookies, or auth headers.
 */

import { expect, test, type Page } from '@playwright/test';

type LiveEnv = {
  username: string;
  password: string;
  executionId: string;
  activeExecutionId: string | null;
};

function readLiveEnv(): LiveEnv | null {
  const base = process.env.PLAYWRIGHT_BASE_URL?.trim();
  const username = process.env.MCPFLOW_E2E_USERNAME?.trim();
  const password = process.env.MCPFLOW_E2E_PASSWORD?.trim();
  const executionId = process.env.MCPFLOW_E2E_EXECUTION_ID?.trim();
  const activeExecutionId =
    process.env.MCPFLOW_E2E_ACTIVE_EXECUTION_ID?.trim() || null;
  if (!base || !username || !password || !executionId) {
    return null;
  }
  return { username, password, executionId, activeExecutionId };
}

const liveEnv = readLiveEnv();

/** Safe pathname only — never dump query/cookie/authorization. */
function eventsPath(executionId: string): string {
  return `/api/v1/executions/${executionId}/events`;
}

function isEventsUrl(url: string, executionId: string): boolean {
  try {
    const u = new URL(url);
    return u.pathname === eventsPath(executionId);
  } catch {
    return false;
  }
}

function isExecutionDetailGet(url: string, executionId: string): boolean {
  try {
    const u = new URL(url);
    return u.pathname === `/api/v1/executions/${executionId}`;
  } catch {
    return false;
  }
}

type SseRequestMeta = {
  path: string;
  lastEventId: string | null;
};

type SseResponseMeta = {
  status: number;
  contentType: string | null;
};

async function realLogin(page: Page, env: LiveEnv): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(/사용자 아이디/i).fill(env.username);
  await page.getByLabel(/비밀번호/i).fill(env.password);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });
}

async function openEventsTab(page: Page): Promise<void> {
  await page.getByRole('button', { name: /^Events$/i }).click();
  await expect(page.getByTestId('sse-connection-state')).toBeVisible({
    timeout: 30_000,
  });
}

async function waitForLive(page: Page, timeoutMs = 45_000): Promise<void> {
  await expect(page.getByTestId('sse-connection-state')).toHaveText('Live', {
    timeout: timeoutMs,
  });
}

async function collectTimelineSseIds(page: Page): Promise<string[]> {
  // Prefer the dedicated "id <decimal>" child text. Whole-row textContent can
  // concatenate adjacent spans without whitespace (e.g. "execution.createdid 1"),
  // which breaks /\bid\s+(\d+)\b/.
  const rows = page.getByTestId('execution-event-row');
  const count = await rows.count();
  const ids: string[] = [];

  for (let i = 0; i < count; i += 1) {
    const label = await rows.nth(i).getByText(/^id \d+$/).textContent();
    const match = label?.match(/^id (\d+)$/);
    if (match) ids.push(match[1]);
  }

  return ids;
}

async function readHeaderStatus(
  page: Page,
  executionId: string,
): Promise<string | null> {
  const text = await page
    .getByRole('heading', { name: executionId })
    .locator('xpath=..')
    .innerText();
  const match = text.match(
    /\b(CREATED|QUEUED|RUNNING|WAITING_INPUT|WAITING_APPROVAL|CANCEL_REQUESTED|SUCCEEDED|PARTIALLY_SUCCEEDED|FAILED|CANCELLED|TIMED_OUT)\b/,
  );
  return match?.[1] ?? null;
}

function attachSseObservers(
  page: Page,
  executionId: string,
  requests: SseRequestMeta[],
  responses: SseResponseMeta[],
): void {
  page.on('request', (req) => {
    if (!isEventsUrl(req.url(), executionId)) return;
    const headers = req.headers();
    requests.push({
      path: eventsPath(executionId),
      lastEventId: headers['last-event-id'] ?? null,
    });
  });
  page.on('response', (res) => {
    if (!isEventsUrl(res.url(), executionId)) return;
    responses.push({
      status: res.status(),
      contentType: res.headers()['content-type'] ?? null,
    });
  });
}

// Real credentials must not land in Playwright traces/videos/screenshots.
test.use({
  trace: 'off',
  video: 'off',
  screenshot: 'off',
});

test.describe('E2E-010 live Traefik SSE', () => {
  test.describe.configure({ mode: 'serial', timeout: 120_000 });

  test.beforeEach(() => {
    test.skip(
      liveEnv == null,
      'Live SSE harness requires PLAYWRIGHT_BASE_URL, MCPFLOW_E2E_USERNAME, MCPFLOW_E2E_PASSWORD, MCPFLOW_E2E_EXECUTION_ID',
    );
  });

  test('real SSE connects Live and shows durable events', async ({ page }) => {
    const env = liveEnv!;
    const sseRequests: SseRequestMeta[] = [];
    const sseResponses: SseResponseMeta[] = [];
    attachSseObservers(page, env.executionId, sseRequests, sseResponses);

    await realLogin(page, env);
    await page.goto(`/executions/${env.executionId}`);
    await expect(page.getByRole('heading', { name: env.executionId })).toBeVisible({
      timeout: 30_000,
    });

    await openEventsTab(page);
    await waitForLive(page);

    await expect
      .poll(() => sseResponses.length, { timeout: 45_000 })
      .toBeGreaterThan(0);
    const firstOk = sseResponses.find((r) => r.status === 200);
    expect(firstOk, 'SSE /events must return HTTP 200').toBeTruthy();
    expect(
      firstOk?.contentType ?? '',
      'SSE Content-Type must be text/event-stream',
    ).toMatch(/text\/event-stream/i);

    const rows = page.getByTestId('execution-event-row');
    const count = await rows.count();
    expect(
      count,
      'target execution must contain durable events created after execution_events rollout',
    ).toBeGreaterThan(0);

    const ids = await collectTimelineSseIds(page);
    expect(ids.length).toBe(count);
    expect(new Set(ids).size).toBe(ids.length);
  });

  test('short offline → native EventSource reconnect to Live', async ({
    page,
    context,
  }) => {
    const env = liveEnv!;
    const sseRequests: SseRequestMeta[] = [];
    const sseResponses: SseResponseMeta[] = [];
    attachSseObservers(page, env.executionId, sseRequests, sseResponses);

    await realLogin(page, env);
    await page.goto(`/executions/${env.executionId}`);
    await expect(page.getByRole('heading', { name: env.executionId })).toBeVisible({
      timeout: 30_000,
    });
    await openEventsTab(page);
    await waitForLive(page);
    await expect(page.getByTestId('execution-event-row').first()).toBeVisible({
      timeout: 45_000,
    });

    const idsBefore = await collectTimelineSseIds(page);
    expect(idsBefore.length).toBeGreaterThan(0);
    const lastAcceptedId = idsBefore[idsBefore.length - 1];
    const requestsBefore = sseRequests.length;

    // Brief offline (< offline grace 5s): allow Reconnecting UI; keep native EventSource.
    await context.setOffline(true);
    await expect(page.getByTestId('sse-connection-state')).toHaveText(
      /Reconnecting|Live|Polling fallback/,
      { timeout: 10_000 },
    );
    await page.waitForTimeout(800);
    await context.setOffline(false);

    await waitForLive(page, 60_000);

    await expect
      .poll(() => sseRequests.length, { timeout: 60_000 })
      .toBeGreaterThan(requestsBefore);

    const reconnectReqs = sseRequests.slice(requestsBefore);
    expect(
      reconnectReqs.length,
      'browser must issue a new /events request after network restore',
    ).toBeGreaterThan(0);

    const withLastEventId = reconnectReqs.find(
      (r) => r.lastEventId != null && /^\d+$/.test(r.lastEventId),
    );
    if (withLastEventId?.lastEventId) {
      expect(withLastEventId.lastEventId).toMatch(/^\d+$/);
      // Prefer exact cursor match when Playwright exposes Last-Event-ID.
      expect(withLastEventId.lastEventId).toBe(lastAcceptedId);
    }
    // If header is unavailable under this Playwright/Chromium build, still
    // require reconnect + Live + no duplicate timeline ids (reported limitation).

    const idsAfter = await collectTimelineSseIds(page);
    expect(new Set(idsAfter).size).toBe(idsAfter.length);
    // Prior ids must remain unique; reconnect must not double-render them.
    for (const id of idsBefore) {
      expect(idsAfter.filter((x) => x === id).length).toBe(1);
    }
  });

  test('active execution sustained offline → Polling fallback + REST poll', async ({
    page,
    context,
  }) => {
    const env = liveEnv!;
    test.skip(
      !env.activeExecutionId,
      'Polling fallback live subtest requires MCPFLOW_E2E_ACTIVE_EXECUTION_ID (non-terminal)',
    );

    const activeId = env.activeExecutionId!;
    const detailGets: string[] = [];
    page.on('request', (req) => {
      if (req.method() === 'GET' && isExecutionDetailGet(req.url(), activeId)) {
        detailGets.push(req.url().split('?')[0] ?? '');
      }
    });

    await realLogin(page, env);
    await page.goto(`/executions/${activeId}`);
    await expect(page.getByRole('heading', { name: activeId })).toBeVisible({
      timeout: 30_000,
    });

    const statusText = await readHeaderStatus(page, activeId);
    const active = new Set([
      'CREATED',
      'QUEUED',
      'RUNNING',
      'WAITING_INPUT',
      'WAITING_APPROVAL',
      'CANCEL_REQUESTED',
    ]);
    if (statusText && !active.has(statusText)) {
      test.skip(
        true,
        'MCPFLOW_E2E_ACTIVE_EXECUTION_ID is terminal; polling fallback not required',
      );
    }

    await openEventsTab(page);
    await waitForLive(page);

    // Sustained browser offline → offline grace watchdog (5s) → Polling fallback.
    // Does not depend on receiving 3 EventSource onerror callbacks (Chromium may
    // keep EventSource "Live" while offline without firing onerror).
    await context.setOffline(true);
    await expect(page.getByTestId('sse-connection-state')).toHaveText(
      'Polling fallback',
      { timeout: 45_000 },
    );

    const detailCountAtFallback = detailGets.length;
    // Network restore: REST polling remains authoritative (no auto SSE return).
    await context.setOffline(false);

    // Polling interval is 4s; allow a couple of cycles once network returns.
    await expect
      .poll(() => detailGets.length, { timeout: 20_000 })
      .toBeGreaterThan(detailCountAtFallback);

    await expect(page.getByRole('heading', { name: activeId })).toBeVisible();
    await expect(page.getByText(/fatal|Unhandled|Application error/i)).toHaveCount(
      0,
    );
  });
});
