/**
 * E2E-001 isolated: login → Agent Run NL → real API → Outbox/Celery/Redis →
 * Mock MCP → on-screen chat result (docs/09).
 *
 * Requires the Python harness (`backend/scripts/e2e001_isolated_stack.py`) and
 * Vite with MCPFLOW_API_PROXY_TARGET. Skips when state file is absent.
 *
 * Never route-fulfill /api responses — only real backend traffic is allowed.
 * PASS also requires Execution/ToolCall SUCCEEDED and Mock MCP tools/call == 1.
 */

import { expect, test, type Page } from '@playwright/test';
import fs from 'node:fs';

type E2E001State = {
  username: string;
  password: string;
  agent_name: string;
  agent_id: string;
  nl_request: string;
  expected_result_fragment: string;
  api_proxy_target: string;
  mcp_stats_url: string;
  run_nonce: string;
};

const STATE_PATH =
  process.env.E2E001_STATE_PATH?.trim() ||
  '/tmp/mcpflow-e2e001/state.json';

function readState(): E2E001State | null {
  if (!fs.existsSync(STATE_PATH)) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(STATE_PATH, 'utf-8')) as E2E001State;
    if (
      !parsed?.expected_result_fragment
      || !parsed?.mcp_stats_url
      || !parsed?.run_nonce
      || !parsed?.api_proxy_target
    ) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

const state = readState();

async function realLogin(page: Page, env: E2E001State): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(/사용자 아이디/i).fill(env.username);
  await page.getByLabel(/비밀번호/i).fill(env.password);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });
}

/** Browser fetch keeps Secure session cookies on http://127.0.0.1 (page.request does not). */
async function browserApiJson(
  page: Page,
  path: string,
): Promise<{ status: number; body: unknown }> {
  return page.evaluate(async (apiPath) => {
    const response = await fetch(apiPath, { credentials: 'include' });
    const text = await response.text();
    let body: unknown = text;
    try {
      body = JSON.parse(text);
    } catch {
      // keep raw text
    }
    return { status: response.status, body };
  }, path);
}

test.describe('E2E-001 NL → Mock MCP (isolated stack)', () => {
  test.skip(
    !state,
    `Missing fresh ${STATE_PATH}. Start backend/scripts/e2e001_isolated_stack.py with explicit E2E001_ALLOW_DB_RESET=1 first.`,
  );

  test('login → Agent Run → result on screen via real API/Celery/Mock MCP', async ({
    page,
  }) => {
    test.setTimeout(180_000);
    const env = state as E2E001State;

    // Guard: never stub internal API — continue to Vite proxy / real API only.
    await page.route('**/api/v1/**', async route => {
      await route.continue();
    });

    await realLogin(page, env);
    await page.goto('/run');
    await expect(page.getByText(/Agent 업무 실행|업무를 요청/i).first()).toBeVisible({
      timeout: 30_000,
    });

    await expect(page.getByText(env.agent_name).first()).toBeVisible({
      timeout: 30_000,
    });

    await page.getByLabel(/업무 지시/i).fill(env.nl_request);
    await page.getByRole('button', { name: /^실행$/i }).click();

    await expect(page.getByText(env.nl_request).first()).toBeVisible({
      timeout: 30_000,
    });

    // Grounded compose-answer must include this run's MCP condition nonce.
    await expect(page.getByText(env.expected_result_fragment).first()).toBeVisible({
      timeout: 120_000,
    });

    const body = await page.locator('body').innerText();
    expect(body).toMatch(/완료|SUCCEEDED|조회 결과/i);

    // Real API evidence — not the fixed ModelOps answer alone.
    const listRes = await browserApiJson(page, '/api/v1/executions?page_size=5');
    expect(listRes.status).toBe(200);
    const listBody = listRes.body as { items: Array<{ id: string }> };
    expect(Array.isArray(listBody.items)).toBeTruthy();
    expect(listBody.items.length).toBeGreaterThanOrEqual(1);
    const executionId = listBody.items[0].id;
    expect(body).toContain(executionId);

    const executionRes = await browserApiJson(
      page,
      `/api/v1/executions/${executionId}`,
    );
    expect(executionRes.status).toBe(200);
    const execution = executionRes.body as {
      status: string;
      source_type: string;
    };
    expect(execution.status).toBe('SUCCEEDED');
    expect(execution.source_type).toBe('AGENT_REQUEST');

    const stepsRes = await browserApiJson(
      page,
      `/api/v1/executions/${executionId}/steps`,
    );
    expect(stepsRes.status).toBe(200);
    const steps = stepsRes.body as {
      items: Array<{ id: string; status: string }>;
    };
    expect(Array.isArray(steps.items)).toBeTruthy();
    expect(steps.items.length).toBeGreaterThanOrEqual(1);
    const stepId = steps.items[0].id;
    expect(steps.items[0].status).toBe('SUCCEEDED');

    const stepDetailRes = await browserApiJson(
      page,
      `/api/v1/executions/${executionId}/steps/${stepId}`,
    );
    expect(stepDetailRes.status).toBe(200);
    const stepDetail = stepDetailRes.body as {
      attempts: Array<{
        status: string;
        tool_calls: Array<{
          normalized_status: string;
          response_bytes: number | null;
        }>;
      }>;
    };
    expect(Array.isArray(stepDetail.attempts)).toBeTruthy();
    expect(stepDetail.attempts.length).toBe(1);
    expect(stepDetail.attempts[0].status).toBe('SUCCEEDED');
    const toolCalls = stepDetail.attempts[0].tool_calls ?? [];
    expect(toolCalls.length).toBe(1);
    expect(toolCalls[0].normalized_status).toBe('SUCCEEDED');
    expect(toolCalls[0].response_bytes).toBeGreaterThan(0);

    // Mock MCP stats are loopback-only (not via /api); no session cookie needed.
    const statsRes = await page.request.get(env.mcp_stats_url);
    expect(statsRes.ok()).toBeTruthy();
    const stats = await statsRes.json();
    expect(stats.tools_call_count).toBe(1);
    expect(stats.expected_result_fragment).toBe(env.expected_result_fragment);
    expect(stats.run_nonce).toBe(env.run_nonce);
  });
});
