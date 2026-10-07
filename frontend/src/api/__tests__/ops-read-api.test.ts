import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import { listAuditEvents, getAuditEvent } from '../audit';
import {
  cancelExecution,
  getExecution,
  getExecutionStep,
  listExecutionSteps,
  listExecutions,
} from '../executions';
import { getDashboardSummary } from '../operations';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Execution / Ops / Audit API contracts', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('listExecutions builds query path and params', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ items: [], page: 1, page_size: 20, total: 0 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await listExecutions({
      page: 2,
      page_size: 20,
      q: 'trace',
      status: 'FAILED,TIMED_OUT',
      source_type: 'AGENT_REQUEST',
      sort: '-requested_at',
    });

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/v1/executions?');
    expect(url).toContain('page=2');
    expect(url).toContain('page_size=20');
    expect(url).toContain('q=trace');
    expect(url).toContain('status=FAILED%2CTIMED_OUT');
    expect(url).toContain('source_type=AGENT_REQUEST');
    expect(url).toContain('sort=-requested_at');
  });

  it('detail/steps/step-detail and cancel paths', async () => {
    setCachedCsrfTokenForTests('csrf');
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ id: 'e1' }))
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({ id: 's1', attempts: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          id: 'e1',
          status: 'CANCEL_REQUESTED',
          cancel_requested_at: '2026-10-07T00:00:00Z',
          finished_at: null,
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    await getExecution('11111111-1111-4111-8111-111111111111');
    await listExecutionSteps('11111111-1111-4111-8111-111111111111');
    await getExecutionStep(
      '11111111-1111-4111-8111-111111111111',
      '22222222-2222-4222-8222-222222222222',
    );
    await cancelExecution('11111111-1111-4111-8111-111111111111');

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      '/api/v1/executions/11111111-1111-4111-8111-111111111111',
    );
    expect(String(fetchMock.mock.calls[1][0])).toBe(
      '/api/v1/executions/11111111-1111-4111-8111-111111111111/steps',
    );
    expect(String(fetchMock.mock.calls[2][0])).toBe(
      '/api/v1/executions/11111111-1111-4111-8111-111111111111/steps/22222222-2222-4222-8222-222222222222',
    );
    expect(String(fetchMock.mock.calls[3][0])).toBe(
      '/api/v1/executions/11111111-1111-4111-8111-111111111111/cancel',
    );
    expect((fetchMock.mock.calls[3][1] as RequestInit).method).toBe('POST');
  });

  it('dashboard summary recent_limit query', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        window_from: '2026-10-06T00:00:00Z',
        window_to: '2026-10-07T00:00:00Z',
        generated_at: '2026-10-07T00:00:00Z',
        executions: {
          total: 0,
          created: 0,
          queued: 0,
          running: 0,
          waiting_input: 0,
          waiting_approval: 0,
          cancel_requested: 0,
          succeeded: 0,
          partially_succeeded: 0,
          failed: 0,
          cancelled: 0,
          timed_out: 0,
        },
        terminal_total: 0,
        success_rate: null,
        avg_duration_ms: null,
        p95_duration_ms: null,
        approvals: { pending: 0, overdue: 0 },
        schedules: { active: 0, paused: 0, completed: 0, error: 0, overdue: 0 },
        mcp_servers: { total: 0, active: 0, inactive: 0, error: 0, draft: 0 },
        mcp_tools: {
          total: 0,
          discovered: 0,
          active: 0,
          inactive: 0,
          missing: 0,
          blocked: 0,
          problematic: 0,
        },
        recent_executions: [],
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await getDashboardSummary({ recent_limit: 5 });
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      '/api/v1/ops/dashboard/summary?',
    );
    expect(String(fetchMock.mock.calls[0][0])).toContain('recent_limit=5');
  });

  it('audit list cursor + detail paths', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: 'abc' }))
      .mockResolvedValueOnce(
        jsonResponse({
          event_id: '33333333-3333-4333-8333-333333333333',
          occurred_at: '2026-10-07T00:00:00Z',
          actor_type: 'USER',
          actor_id: null,
          action: 'execution.create',
          resource_type: null,
          resource_id: null,
          execution_id: null,
          result: 'SUCCESS',
          request_id: null,
          trace_id: null,
          reason: null,
          integrity_hash: 'a'.repeat(64),
          before_data: null,
          after_data: {},
          change_set: null,
          source_ip_hash: null,
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    await listAuditEvents({
      cursor: 'c1',
      limit: 50,
      result: 'SUCCESS',
      actor_type: 'USER',
      execution_id: '11111111-1111-4111-8111-111111111111',
    });
    await getAuditEvent('33333333-3333-4333-8333-333333333333');

    const listUrl = String(fetchMock.mock.calls[0][0]);
    expect(listUrl).toContain('/api/v1/audit/events?');
    expect(listUrl).toContain('cursor=c1');
    expect(listUrl).toContain('result=SUCCESS');
    expect(listUrl).toContain('actor_type=USER');
    expect(listUrl).toContain(
      'execution_id=11111111-1111-4111-8111-111111111111',
    );
    expect(String(fetchMock.mock.calls[1][0])).toBe(
      '/api/v1/audit/events/33333333-3333-4333-8333-333333333333',
    );
  });
});
