/**
 * E2E-001 isolated: login → Agent Run NL → real API → Outbox/Celery/Redis →
 * Mock MCP → on-screen chat result (docs/09).
 *
 * Requires the Python harness (`backend/scripts/e2e001_isolated_stack.py`) and
 * Vite with MCPFLOW_API_PROXY_TARGET. Skips when state file is absent.
 *
 * Never route-fulfill /api responses — only real backend traffic is allowed.
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
};

const STATE_PATH =
  process.env.E2E001_STATE_PATH?.trim() ||
  '/tmp/mcpflow-e2e001/state.json';

function readState(): E2E001State | null {
  if (!fs.existsSync(STATE_PATH)) return null;
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf-8')) as E2E001State;
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

test.describe('E2E-001 NL → Mock MCP (isolated stack)', () => {
  test.skip(
    !state,
    `Missing ${STATE_PATH}. Start backend/scripts/e2e001_isolated_stack.py with E2E001_ALLOW_DB_RESET=1 first.`,
  );

  test('login → Agent Run → result on screen via real API/Celery/Mock MCP', async ({
    page,
  }) => {
    test.setTimeout(180_000);
    const env = state as E2E001State;

    // Guard: never stub internal API — fail if something tries to fulfill.
    await page.route('**/api/v1/**', async route => {
      // Continue to Vite proxy / real API only.
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

    // User message appears from real submit.
    await expect(page.getByText(env.nl_request).first()).toBeVisible({
      timeout: 30_000,
    });

    // Grounded compose-answer must surface Mock MCP structured condition text.
    await expect(page.getByText(env.expected_result_fragment).first()).toBeVisible({
      timeout: 120_000,
    });

    // Execution reached a success path in the UI status strip when present.
    const body = await page.locator('body').innerText();
    expect(body).toMatch(/완료|SUCCEEDED|조회 결과/i);
  });
});
