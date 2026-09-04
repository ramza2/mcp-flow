import { expect, test } from '@playwright/test';

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

for (const { path, expectText } of ROUTES) {
  test(`smoke: ${path} loads`, async ({ page }) => {
    const response = await page.goto(path);
    expect(response?.ok() ?? true).toBeTruthy();
    await expect(page.locator('body')).not.toContainText(/Something went wrong|Application Error|Fatal/i);
    await expect(page.locator('body')).toContainText(expectText);
  });
}
