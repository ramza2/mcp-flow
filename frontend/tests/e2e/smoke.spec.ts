import { expect, test } from '@playwright/test';
import {
  activeServer,
  discoveredTool,
  discoveryList,
  serverList,
  toolList,
  validVersion,
  versionList,
} from '../fixtures/mcp-api';

/**
 * Smoke E2E only — verify routes load without fatal errors.
 * Do not replay full Mock workflows here.
 */
const ROUTES: Array<{ path: string; expectText: RegExp }> = [
  { path: '/', expectText: /Dashboard|MCPFlow|최근|실행/i },
  { path: '/run', expectText: /Agent Run|Plan \/ Execution|업무를 요청/i },
  { path: '/executions', expectText: /Executions|Execution/i },
  { path: '/approvals', expectText: /Approvals|Approval/i },
  { path: '/schedules', expectText: /Schedules|Schedule/i },
  { path: '/agents', expectText: /Agents|Agent/i },
  { path: '/workflows', expectText: /Workflows|Workflow/i },
  { path: '/mcp/servers', expectText: /MCP Servers|Register MCP/i },
  { path: '/mcp/tools', expectText: /MCP Tools|Tool/i },
  { path: '/admin/model-profiles', expectText: /Model Profile/i },
];

async function stubMcpApi(page: import('@playwright/test').Page) {
  await page.route('**/api/v1/mcp/**', async route => {
    const url = route.request().url();
    const method = route.request().method();

    if (url.includes('/mcp/servers') && method === 'GET' && !url.match(/\/mcp\/servers\/[^/?]+/)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(serverList) });
    }
    if (url.includes(`/mcp/servers/${activeServer.id}`) && method === 'GET' && !url.includes('/tools') && !url.includes('/discoveries')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(activeServer) });
    }
    if (url.includes(`/mcp/servers/${activeServer.id}/tools`)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(toolList) });
    }
    if (url.includes(`/mcp/servers/${activeServer.id}/discoveries`)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(discoveryList) });
    }
    if (url.includes('/mcp/tools') && method === 'GET' && !url.match(/\/mcp\/tools\/[^/?]+/)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(toolList) });
    }
    if (url.includes(`/mcp/tools/${discoveredTool.id}`) && !url.includes('/versions')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(discoveredTool) });
    }
    if (url.includes(`/mcp/tools/${discoveredTool.id}/versions`) && url.includes(validVersion.id)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(validVersion) });
    }
    if (url.includes(`/mcp/tools/${discoveredTool.id}/versions`)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(versionList) });
    }

    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'NOT_FOUND', message: 'not stubbed' } }) });
  });
}

for (const { path, expectText } of ROUTES) {
  test(`smoke: ${path} loads`, async ({ page }) => {
    if (path.startsWith('/mcp/')) {
      await stubMcpApi(page);
    }
    const response = await page.goto(path);
    expect(response?.ok() ?? true).toBeTruthy();
    await expect(page.locator('body')).not.toContainText(/Something went wrong|Application Error|Fatal/i);
    await expect(page.locator('body')).toContainText(expectText);
  });
}

test('smoke: /mcp/servers/:id loads with API stub', async ({ page }) => {
  await stubMcpApi(page);
  const response = await page.goto(`/mcp/servers/${activeServer.id}`);
  expect(response?.ok() ?? true).toBeTruthy();
  await expect(page.locator('body')).toContainText(/Docs MCP/i);
  await expect(page.locator('body')).not.toContainText(/데이터를 불러오지 못했습니다/i);
});

test('smoke: /mcp/tools/:id loads with API stub', async ({ page }) => {
  await stubMcpApi(page);
  const response = await page.goto(`/mcp/tools/${discoveredTool.id}`);
  expect(response?.ok() ?? true).toBeTruthy();
  await expect(page.locator('body')).toContainText(/Search Docs/i);
  await expect(page.locator('body')).not.toContainText(/데이터를 불러오지 못했습니다/i);
});
