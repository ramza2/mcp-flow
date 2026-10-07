/**
 * ExecutionDetail contracts — real UUID routes + mocked API responses.
 * Mock EXE-* fixture IDs are intentionally retired.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ExecutionDetail from '@/screens/work/ExecutionDetail';
import { renderWithRouter } from '../test-utils';
import { setCachedCsrfTokenForTests } from '@/api/csrf';

const EXEC_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const STEP_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function baseDetail(status: string) {
  return {
    id: EXEC_ID,
    source_type: 'MANUAL_TOOL_TEST',
    trigger_type: 'TEST',
    requester_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    agent_request_id: null,
    agent_version_id: null,
    workflow_version_id: null,
    schedule_occurrence_id: null,
    parent_execution_id: null,
    status,
    error_code: null,
    error_category: null,
    trace_id: null,
    requested_at: '2026-10-07T12:00:00Z',
    queued_at: '2026-10-07T12:00:00Z',
    started_at: '2026-10-07T12:00:01Z',
    finished_at: null,
    cancel_requested_at: null,
    step_count: 1,
    completed_step_count: 0,
    failed_step_count: 0,
    duration_ms: 1000,
    source: {
      type: 'MANUAL_TOOL_TEST',
      version_id: null,
      logical_id: null,
      name: null,
    },
    plan_schema_version: '1.0',
    plan_hash: 'e'.repeat(64),
    plan_limits: null,
    result_summary: null,
    retention_until: null,
  };
}

describe('ExecutionDetail MRTR / UNKNOWN_OUTCOME (API-backed)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('WAITING_INPUT uses MrtrInputPanel without requestState exposure', async () => {
    setCachedCsrfTokenForTests('csrf');
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.endsWith(`/executions/${EXEC_ID}`)) {
        return Promise.resolve(jsonResponse(baseDetail('WAITING_INPUT')));
      }
      if (u.endsWith('/steps')) return Promise.resolve(jsonResponse({ items: [] }));
      if (u.includes('/input-requests')) {
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
                status: 'OPEN',
                source: 'MCP_MRTR',
                execution_id: EXEC_ID,
                step_execution_id: STEP_ID,
                round_no: 1,
                input_requests: {
                  city: { type: 'string', description: 'City name' },
                },
                expires_at: '2026-10-07T13:00:00Z',
                requested_at: '2026-10-07T12:00:00Z',
                answered_at: null,
              },
            ],
          }),
        );
      }
      return Promise.resolve(jsonResponse({ error: { code: 'NOT_FOUND', message: 'x' } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });

    expect(await screen.findByText(/City name/i)).toBeInTheDocument();
    expect(screen.queryByDisplayValue(/requestState/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /requestState/i })).not.toBeInTheDocument();
  });

  it('UNKNOWN_OUTCOME shows ops guidance and hides Retry CTA', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.endsWith(`/executions/${EXEC_ID}`)) {
        return Promise.resolve(jsonResponse(baseDetail('FAILED')));
      }
      if (u.endsWith('/steps')) {
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                id: STEP_ID,
                execution_id: EXEC_ID,
                step_key: 'send',
                step_type: 'TOOL',
                parent_step_id: null,
                sequence_hint: 1,
                mcp_tool_version_id: null,
                iteration_no: null,
                status: 'UNKNOWN_OUTCOME',
                attempt_count: 1,
                condition_result: null,
                ready_at: null,
                started_at: '2026-10-07T12:00:01Z',
                finished_at: null,
                error_code: 'TIMEOUT',
                error_category: 'timeout',
                duration_ms: null,
              },
            ],
          }),
        );
      }
      if (u.includes(`/steps/${STEP_ID}`)) {
        return Promise.resolve(
          jsonResponse({
            id: STEP_ID,
            execution_id: EXEC_ID,
            step_key: 'send',
            step_type: 'TOOL',
            parent_step_id: null,
            sequence_hint: 1,
            mcp_tool_version_id: null,
            iteration_no: null,
            status: 'UNKNOWN_OUTCOME',
            attempt_count: 1,
            condition_result: null,
            ready_at: null,
            started_at: '2026-10-07T12:00:01Z',
            finished_at: null,
            error_code: 'TIMEOUT',
            error_category: 'timeout',
            duration_ms: null,
            attempts: [],
          }),
        );
      }
      return Promise.resolve(jsonResponse({ error: { code: 'NOT_FOUND', message: 'x' } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });

    expect(await screen.findByText(/UNKNOWN_OUTCOME/i)).toBeInTheDocument();
    expect(screen.getByText(/자동 Retry CTA는 제공하지 않습니다/i)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /New Execution \(Retry\)/i }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Steps$/i }));
    await user.click(screen.getByText('send'));
    await waitFor(() => {
      expect(screen.getByText(/자동 Retry CTA 없음/i)).toBeInTheDocument();
    });
  });
});
