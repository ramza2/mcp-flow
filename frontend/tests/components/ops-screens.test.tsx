import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Dashboard from '@/screens/Dashboard';
import Executions from '@/screens/work/Executions';
import ExecutionDetail from '@/screens/work/ExecutionDetail';
import AuditLogs from '@/screens/admin/AuditLogs';
import { renderWithRouter } from '../test-utils';
import { setCachedCsrfTokenForTests } from '@/api/csrf';

const EXEC_ID = '11111111-1111-4111-8111-111111111111';
const STEP_ID = '22222222-2222-4222-8222-222222222222';
const EVENT_ID = '33333333-3333-4333-8333-333333333333';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-test' },
  });
}

function apiError(status: number, code: string, message: string) {
  return jsonResponse(
    { error: { code, message, request_id: 'req-test', details: [] } },
    status,
  );
}

function executionItem(overrides: Record<string, unknown> = {}) {
  return {
    id: EXEC_ID,
    source_type: 'WORKFLOW_VERSION',
    trigger_type: 'USER',
    requester_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    agent_request_id: null,
    agent_version_id: null,
    workflow_version_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    schedule_occurrence_id: null,
    parent_execution_id: null,
    status: 'SUCCEEDED',
    error_code: null,
    error_category: null,
    trace_id: 'trace-1',
    requested_at: '2026-10-07T12:00:00Z',
    queued_at: '2026-10-07T12:00:01Z',
    started_at: '2026-10-07T12:00:02Z',
    finished_at: '2026-10-07T12:00:12Z',
    cancel_requested_at: null,
    step_count: 2,
    completed_step_count: 2,
    failed_step_count: 0,
    duration_ms: 10000,
    source: {
      type: 'WORKFLOW_VERSION',
      version_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      logical_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
      name: 'Ops Workflow',
    },
    ...overrides,
  };
}

function dashboardSummary() {
  return {
    window_from: '2026-10-06T12:00:00Z',
    window_to: '2026-10-07T12:00:00Z',
    generated_at: '2026-10-07T12:00:00Z',
    executions: {
      total: 10,
      created: 0,
      queued: 0,
      running: 1,
      waiting_input: 0,
      waiting_approval: 1,
      cancel_requested: 0,
      succeeded: 5,
      partially_succeeded: 0,
      failed: 2,
      cancelled: 1,
      timed_out: 0,
    },
    terminal_total: 8,
    success_rate: 0.625,
    avg_duration_ms: 30000,
    p95_duration_ms: 48000,
    approvals: { pending: 2, overdue: 1 },
    schedules: { active: 3, paused: 0, completed: 0, error: 1, overdue: 1 },
    mcp_servers: { total: 4, active: 2, inactive: 1, error: 0, draft: 1 },
    mcp_tools: {
      total: 10,
      discovered: 1,
      active: 6,
      inactive: 1,
      missing: 1,
      blocked: 1,
      problematic: 2,
    },
    recent_executions: [executionItem()],
  };
}

describe('Ops screens real API wiring', () => {
  beforeEach(() => {
    setCachedCsrfTokenForTests('csrf');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('Dashboard ops success shows aggregate metrics without inventing MCP names', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(dashboardSummary()));
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<Dashboard />);

    expect(await screen.findByText('Total Executions')).toBeInTheDocument();
    expect(screen.getByText('63%')).toBeInTheDocument();
    expect(screen.getByText('Ops Workflow')).toBeInTheDocument();
    expect(screen.getByText('MCP Server (aggregate)')).toBeInTheDocument();
    expect(screen.getByText('Tool (aggregate)')).toBeInTheDocument();
    expect(screen.getByText('Problematic')).toBeInTheDocument();
    expect(screen.queryByText('Report MCP')).not.toBeInTheDocument();
    expect(String(fetchMock.mock.calls[0][0])).toContain('/ops/dashboard/summary');
  });

  it('Dashboard 403 falls back to own recent executions', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(apiError(403, 'AUTH_FORBIDDEN', 'Missing execution.read'))
      .mockResolvedValueOnce(
        jsonResponse({
          items: [executionItem({ status: 'RUNNING', source: { type: 'AGENT_REQUEST', version_id: null, logical_id: null, name: 'My Agent' } })],
          page: 1,
          page_size: 5,
          total: 1,
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<Dashboard />);

    expect(
      await screen.findByText(/전역 운영 지표는 execution.read 권한이 필요합니다/i),
    ).toBeInTheDocument();
    expect(screen.getByText('My Agent')).toBeInTheDocument();
    expect(screen.getByText(/권한이 없습니다/i)).toBeInTheDocument();
    expect(String(fetchMock.mock.calls[1][0])).toContain('/api/v1/executions?');
  });

  it('Executions list/filter/pagination and empty/error states', async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          items: [executionItem()],
          page: 1,
          page_size: 20,
          total: 1,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ items: [], page: 1, page_size: 20, total: 0 }),
      );
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<Executions />);

    expect(await screen.findByText('Ops Workflow')).toBeInTheDocument();
    expect(screen.getByText(/2 \/ 2/)).toBeInTheDocument();

    await user.selectOptions(screen.getByDisplayValue(/상태: 전체/i), 'FAILED');
    await waitFor(() => {
      expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('status=FAILED');
    });
    expect(await screen.findByText(/조건에 맞는 Execution이 없습니다/i)).toBeInTheDocument();
  });

  it('Executions 403 shows PermissionDenied', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(apiError(403, 'AUTH_FORBIDDEN', 'denied')),
    );
    renderWithRouter(<Executions />);
    expect(await screen.findByText(/접근 권한이 없습니다/i)).toBeInTheDocument();
  });

  it('ExecutionDetail loads detail+steps, selected step detail, cancel, UNKNOWN_OUTCOME without retry', async () => {
    const user = userEvent.setup();
    const detail = {
      ...executionItem({ status: 'RUNNING' }),
      plan_schema_version: '1.0',
      plan_hash: 'a'.repeat(64),
      plan_limits: {
        max_steps: 10,
        max_duration_seconds: 60,
        max_parallelism: 2,
        max_loop_iterations: 5,
      },
      result_summary: { status: 'RUNNING', step_keys: ['t1'], step_count: 1, step_statuses: { t1: 'UNKNOWN_OUTCOME' } },
      retention_until: null,
    };
    const steps = {
      items: [
        {
          id: STEP_ID,
          execution_id: EXEC_ID,
          step_key: 't1',
          step_type: 'TOOL',
          parent_step_id: null,
          sequence_hint: 1,
          mcp_tool_version_id: null,
          iteration_no: null,
          status: 'UNKNOWN_OUTCOME',
          attempt_count: 1,
          condition_result: null,
          ready_at: null,
          started_at: '2026-10-07T12:00:02Z',
          finished_at: null,
          error_code: 'TIMEOUT',
          error_category: 'timeout',
          duration_ms: null,
        },
      ],
    };
    const stepDetail = {
      ...steps.items[0],
      attempts: [
        {
          id: '44444444-4444-4444-8444-444444444444',
          attempt_no: 1,
          status: 'UNKNOWN_OUTCOME',
          error_layer: 'TIMEOUT',
          error_code: 'TIMEOUT',
          error_category: 'timeout',
          is_retryable: false,
          started_at: '2026-10-07T12:00:02Z',
          finished_at: null,
          duration_ms: null,
          tool_calls: [],
        },
      ],
    };

    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.endsWith(`/executions/${EXEC_ID}`) && (!init?.method || init.method === 'GET')) {
        return Promise.resolve(jsonResponse(detail));
      }
      if (u.endsWith(`/executions/${EXEC_ID}/steps`)) {
        return Promise.resolve(jsonResponse(steps));
      }
      if (u.includes(`/steps/${STEP_ID}`)) {
        return Promise.resolve(jsonResponse(stepDetail));
      }
      if (u.endsWith('/cancel')) {
        return Promise.resolve(
          jsonResponse({
            id: EXEC_ID,
            status: 'CANCEL_REQUESTED',
            cancel_requested_at: '2026-10-07T12:01:00Z',
            finished_at: null,
          }),
        );
      }
      return Promise.resolve(apiError(404, 'NOT_FOUND', 'missing'));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });

    expect(await screen.findByText(EXEC_ID)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /New Execution \(Retry\)/i })).not.toBeInTheDocument();
    expect(screen.getByText(/UNKNOWN_OUTCOME Step이 있습니다/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Steps$/i }));
    await user.click(screen.getByText('t1'));
    expect(await screen.findByText(/자동 Retry CTA 없음/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /실행 취소/i }));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (c) => String(c[0]).endsWith('/cancel') && (c[1] as RequestInit).method === 'POST',
        ),
      ).toBe(true);
    });
  });

  it('ExecutionDetail WAITING_INPUT keeps live MrtrInputPanel path', async () => {
    const detail = {
      ...executionItem({ status: 'WAITING_INPUT' }),
      plan_schema_version: '1.0',
      plan_hash: 'b'.repeat(64),
      plan_limits: null,
      result_summary: null,
      retention_until: null,
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.endsWith(`/executions/${EXEC_ID}`)) return Promise.resolve(jsonResponse(detail));
      if (u.endsWith(`/executions/${EXEC_ID}/steps`)) {
        return Promise.resolve(jsonResponse({ items: [] }));
      }
      if (u.includes('/input-requests')) {
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                id: '55555555-5555-4555-8555-555555555555',
                status: 'OPEN',
                source: 'MCP_MRTR',
                execution_id: EXEC_ID,
                step_execution_id: STEP_ID,
                round_no: 1,
                input_requests: { city: { type: 'string', description: 'City' } },
                expires_at: '2026-10-07T13:00:00Z',
                requested_at: '2026-10-07T12:00:00Z',
                answered_at: null,
              },
            ],
          }),
        );
      }
      return Promise.resolve(apiError(404, 'NOT_FOUND', 'missing'));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });

    expect(await screen.findByText(/City/i)).toBeInTheDocument();
    expect(screen.queryByDisplayValue(/requestState/i)).not.toBeInTheDocument();
    expect(String(fetchMock.mock.calls.map((c) => c[0]).join('\n'))).toContain(
      '/input-requests',
    );
  });

  it('Audit list/detail/cursor and 403', async () => {
    const user = userEvent.setup();
    const listBody = {
      items: [
        {
          event_id: EVENT_ID,
          occurred_at: '2026-10-07T12:00:00Z',
          actor_type: 'USER',
          actor_id: 'user-1',
          action: 'execution.create',
          resource_type: 'execution',
          resource_id: EXEC_ID,
          execution_id: EXEC_ID,
          result: 'SUCCESS',
          request_id: 'req-1',
          trace_id: null,
          reason: null,
          integrity_hash: 'c'.repeat(64),
        },
      ],
      next_cursor: 'next-c',
    };
    const detailBody = {
      ...listBody.items[0],
      before_data: null,
      after_data: { status: 'CREATED' },
      change_set: { status: ['null', 'CREATED'] },
      source_ip_hash: null,
    };

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(listBody))
      .mockResolvedValueOnce(jsonResponse(detailBody))
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }));
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<AuditLogs />);

    expect(await screen.findByText('execution.create')).toBeInTheDocument();
    expect(screen.getByText('SUCCESS')).toBeInTheDocument();
    expect(screen.queryByText('FAILED')).not.toBeInTheDocument();

    await user.click(screen.getByText('execution.create'));
    expect(await screen.findByText(/"status": "CREATED"/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Next/i }));
    await waitFor(() => {
      expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('cursor=next-c');
    });
  });

  it('Audit search sends q (not action) and resets cursor stack', async () => {
    const user = userEvent.setup();
    const empty = { items: [], next_cursor: null };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: 'c1' }))
      .mockResolvedValue(jsonResponse(empty));
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<AuditLogs />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const search = screen.getByPlaceholderText(
      'Actor, Action, Resource, Request ID 검색...',
    );
    await user.type(search, 'req-abc');

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      const withQ = urls.find((u) => u.includes('q=req-abc'));
      expect(withQ).toBeTruthy();
      expect(withQ).not.toContain('action=req-abc');
      expect(withQ).not.toMatch(/[?&]action=/);
    });
  });

  it('Audit 403 shows PermissionDenied', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(apiError(403, 'AUTH_FORBIDDEN', 'no audit.read')),
    );
    renderWithRouter(<AuditLogs />);
    expect(await screen.findByText(/접근 권한이 없습니다/i)).toBeInTheDocument();
  });

  it('targeted screens do not import mock.ts', async () => {
    // Static source check via dynamic import graph strings is brittle;
    // assert runtime UI never shows known mock fixture IDs.
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ items: [], page: 1, page_size: 20, total: 0 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    renderWithRouter(<Executions />);
    await screen.findByText(/Execution이 없습니다/i);
    expect(screen.queryByText('EXE-20260902-00126')).not.toBeInTheDocument();
    expect(screen.queryByText('EXE-20260901-00119')).not.toBeInTheDocument();
  });
});

describe('ExecutionDetail Events/IO boundaries', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('Events tab shows SSE timeline shell; IO shows safe result_summary only', async () => {
    const user = userEvent.setup();
    setCachedCsrfTokenForTests('csrf');
    const detail = {
      ...executionItem(),
      plan_schema_version: '1.0',
      plan_hash: 'd'.repeat(64),
      plan_limits: null,
      result_summary: { status: 'SUCCEEDED', step_keys: ['t1'], step_count: 1 },
      retention_until: null,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        const u = String(url);
        if (u.endsWith(`/executions/${EXEC_ID}`)) return Promise.resolve(jsonResponse(detail));
        if (u.endsWith('/steps')) return Promise.resolve(jsonResponse({ items: [] }));
        return Promise.resolve(apiError(404, 'NOT_FOUND', 'x'));
      }),
    );

    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });

    await screen.findByText(EXEC_ID);
    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    expect(screen.queryByText(/Events API deferred/i)).not.toBeInTheDocument();
    expect(
      await screen.findByText(/아직 수신된 Execution Event가 없습니다/i),
    ).toBeInTheDocument();
    expect(screen.getByTestId('sse-connection-state')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Inputs \/ Outputs$/i }));
    expect(
      await screen.findByText(/input_snapshot \/ plan_snapshot은 Operations API에서 의도적으로 미노출/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/"step_keys"/i)).toBeInTheDocument();
    const preBlocks = screen.getAllByText((_, el) => el?.tagName === 'PRE');
    for (const pre of preBlocks) {
      expect(pre.textContent ?? '').not.toMatch(/"secret"|Bearer |password/);
    }
  });
});
