import { expect, test } from '@playwright/test';
import { mockAuthSession, sessionUnauthorizedBody } from '../fixtures/auth-api';

async function stubAuth(page: import('@playwright/test').Page, opts: {
  session?: 'ok' | 'unauthorized';
  login?: 'ok' | 'fail';
}) {
  let sessionMode = opts.session ?? 'unauthorized';
  await page.route('**/api/v1/auth/session', async route => {
    if (sessionMode === 'unauthorized') {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify(sessionUnauthorizedBody()),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockAuthSession),
    });
  });
  await page.route('**/api/v1/auth/login', async route => {
    if (opts.login === 'fail') {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          error: { code: 'AUTH_INVALID_CREDENTIALS', message: 'Invalid username or password' },
        }),
      });
    }
    sessionMode = 'ok';
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockAuthSession),
    });
  });
  await page.route('**/api/v1/auth/csrf', async route => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ csrf_token: 'e2e-csrf' }),
    });
  });
  await page.route('**/api/v1/auth/logout', async route => {
    sessionMode = 'unauthorized';
    return route.fulfill({ status: 204, body: '' });
  });
  await page.route('**/api/v1/mcp/tools**', async route => {
    if (sessionMode === 'unauthorized') {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify(sessionUnauthorizedBody()),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
    });
  });
}

test('auth e2e: unauthenticated /mcp/tools redirects to /login', async ({ page }) => {
  await stubAuth(page, { session: 'unauthorized' });
  await page.goto('/mcp/tools');
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole('heading', { name: /로그인/i })).toBeVisible();
});

test('auth e2e: login success reaches protected screen', async ({ page }) => {
  await stubAuth(page, { session: 'unauthorized', login: 'ok' });
  await page.goto('/login');
  await page.getByLabel(/사용자 아이디/i).fill('demo');
  await page.getByLabel(/비밀번호/i).fill('correct-horse-battery');
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText('Demo User').first()).toBeVisible();
});

test('auth e2e: intended route restore after login', async ({ page }) => {
  await stubAuth(page, { session: 'unauthorized', login: 'ok' });
  await page.goto('/mcp/tools');
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel(/사용자 아이디/i).fill('demo');
  await page.getByLabel(/비밀번호/i).fill('correct-horse-battery');
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/mcp\/tools/);
  await expect(page.locator('body')).toContainText(/MCP Tools|Tool/i);
});

test('auth e2e: expired session on protected API redirects to login', async ({ page }) => {
  let sessionOk = true;
  await page.route('**/api/v1/auth/session', async route => {
    if (!sessionOk) {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify(sessionUnauthorizedBody()),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockAuthSession),
    });
  });
  await page.route('**/api/v1/auth/csrf', async route => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ csrf_token: 'e2e-csrf' }),
    });
  });
  await page.route('**/api/v1/mcp/tools**', async route => {
    sessionOk = false;
    return route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify(sessionUnauthorizedBody()),
    });
  });

  await page.goto('/mcp/tools');
  await expect(page).toHaveURL(/\/login/);
});
