/**
 * ExecutionDetail — REST snapshot → SSE → polling fallback contracts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ExecutionDetail from '@/screens/work/ExecutionDetail';
import {
  SNAPSHOT_REFRESH_DEBOUNCE_MS,
  SSE_OFFLINE_FALLBACK_GRACE_MS,
} from '@/api/executionEvents';
import { renderWithRouter } from '../test-utils';
import { setCachedCsrfTokenForTests } from '@/api/csrf';
import { FakeEventSource, stubFakeEventSource } from '../helpers/fakeEventSource';

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
    finished_at: status === 'SUCCEEDED' ? '2026-10-07T12:00:10Z' : null,
    cancel_requested_at:
      status === 'CANCEL_REQUESTED' ? '2026-10-07T12:00:05Z' : null,
    step_count: 1,
    completed_step_count: status === 'SUCCEEDED' ? 1 : 0,
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

function baseStep(status = 'RUNNING') {
  return {
    id: STEP_ID,
    execution_id: EXEC_ID,
    step_key: 'send',
    step_type: 'TOOL',
    parent_step_id: null,
    sequence_hint: 1,
    mcp_tool_version_id: null,
    iteration_no: null,
    status,
    attempt_count: 1,
    condition_result: null,
    ready_at: null,
    started_at: '2026-10-07T12:00:01Z',
    finished_at: null,
    error_code: null,
    error_category: null,
    duration_ms: null,
  };
}

function envelope(eventType: string, status?: string) {
  return {
    event_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
    execution_id: EXEC_ID,
    step_execution_id: STEP_ID,
    event_type: eventType,
    payload: status ? { status } : {},
    payload_version: 1,
    occurred_at: '2026-10-07T12:00:02Z',
  };
}

async function waitForSse() {
  await waitFor(() => {
    expect(FakeEventSource.instances.length).toBeGreaterThan(0);
  });
  return FakeEventSource.instances[FakeEventSource.instances.length - 1];
}

describe('ExecutionDetail MRTR / UNKNOWN_OUTCOME (API-backed)', () => {
  beforeEach(() => {
    stubFakeEventSource();
    setCachedCsrfTokenForTests('csrf');
  });

  afterEach(() => {
    FakeEventSource.reset();
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('WAITING_INPUT uses MrtrInputPanel without requestState exposure', async () => {
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
                ...baseStep('UNKNOWN_OUTCOME'),
                error_code: 'TIMEOUT',
                error_category: 'timeout',
              },
            ],
          }),
        );
      }
      if (u.includes(`/steps/${STEP_ID}`)) {
        return Promise.resolve(
          jsonResponse({
            ...baseStep('UNKNOWN_OUTCOME'),
            error_code: 'TIMEOUT',
            error_category: 'timeout',
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

describe('ExecutionDetail SSE + polling fallback', () => {
  beforeEach(() => {
    stubFakeEventSource();
    setCachedCsrfTokenForTests('csrf');
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    FakeEventSource.reset();
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  function mockRunningFetch(overrides?: {
    detailStatus?: string;
    stepStatus?: string;
    onDetail?: () => void;
  }) {
    let detailStatus = overrides?.detailStatus ?? 'RUNNING';
    let stepStatus = overrides?.stepStatus ?? 'RUNNING';
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.endsWith(`/executions/${EXEC_ID}`)) {
        overrides?.onDetail?.();
        return Promise.resolve(jsonResponse(baseDetail(detailStatus)));
      }
      if (u.endsWith('/steps')) {
        return Promise.resolve(jsonResponse({ items: [baseStep(stepStatus)] }));
      }
      if (u.includes(`/steps/${STEP_ID}`)) {
        return Promise.resolve(
          jsonResponse({ ...baseStep(stepStatus), attempts: [] }),
        );
      }
      if (u.includes('/input-requests')) {
        return Promise.resolve(jsonResponse({ items: [] }));
      }
      return Promise.resolve(jsonResponse({ error: { code: 'NOT_FOUND', message: 'x' } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    return {
      fetchMock,
      setDetailStatus: (s: string) => {
        detailStatus = s;
      },
      setStepStatus: (s: string) => {
        stepStatus = s;
      },
    };
  }

  it('connects EventSource after REST snapshot with correct URL', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mockRunningFetch();
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    expect(await screen.findByText(EXEC_ID)).toBeInTheDocument();
    const source = await waitForSse();
    expect(source.url).toBe(`/api/v1/executions/${EXEC_ID}/events`);
    act(() => source.emitOpen());
    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent('Live');
  });

  it('named SSE event triggers quiet snapshot refresh without status regression', async () => {
    const { fetchMock } = mockRunningFetch();
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());

    const detailCallsBefore = fetchMock.mock.calls.filter((c) =>
      String(c[0]).endsWith(`/executions/${EXEC_ID}`),
    ).length;

    // Replay created must NOT apply CREATED onto UI — only invalidate snapshot.
    act(() => {
      source.emitNamed('execution.created', envelope('execution.created', 'CREATED'), '10');
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SNAPSHOT_REFRESH_DEBOUNCE_MS + 10);
    });

    await waitFor(() => {
      const detailCalls = fetchMock.mock.calls.filter((c) =>
        String(c[0]).endsWith(`/executions/${EXEC_ID}`),
      ).length;
      expect(detailCalls).toBeGreaterThan(detailCallsBefore);
    });
    // Still RUNNING from REST authority (mock unchanged).
    const runningBadges = screen.getAllByText('RUNNING');
    expect(runningBadges.length).toBeGreaterThan(0);
    expect(screen.queryByText('CREATED')).not.toBeInTheDocument();
  });

  it('ignores onmessage-only delivery (requires named listeners)', async () => {
    mockRunningFetch();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());
    act(() => {
      source.emitMessage(envelope('execution.started', 'RUNNING'), '5');
    });
    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    expect(screen.queryByTestId('execution-event-row')).not.toBeInTheDocument();
  });

  it('duplicate and lower SSE ids are ignored; increasing id appends timeline', async () => {
    mockRunningFetch();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());

    act(() => {
      source.emitNamed('execution.queued', envelope('execution.queued', 'QUEUED'), '20');
      source.emitNamed('execution.queued', envelope('execution.queued', 'QUEUED'), '20');
      source.emitNamed('execution.created', envelope('execution.created', 'CREATED'), '5');
      source.emitNamed('execution.started', envelope('execution.started', 'RUNNING'), '21');
    });

    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    const rows = await screen.findAllByTestId('execution-event-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText(/execution\.queued/)).toBeInTheDocument();
    expect(within(rows[1]).getByText(/execution\.started/)).toBeInTheDocument();
    expect(screen.queryByText('Events API deferred')).not.toBeInTheDocument();
  });

  it('malformed lastEventId is not applied', async () => {
    mockRunningFetch();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => {
      source.emitNamed('execution.started', envelope('execution.started'), '');
      source.emitNamed('execution.started', envelope('execution.started'), 'abc');
    });
    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    expect(screen.queryByTestId('execution-event-row')).not.toBeInTheDocument();
  });

  it('transient errors keep EventSource open; repeated errors fall back to polling', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { fetchMock } = mockRunningFetch();
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());
    await user.click(screen.getByRole('button', { name: /^Events$/i }));

    act(() => {
      source.emitError();
      source.emitError();
    });
    expect(source.isClosed).toBe(false);
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent(
      'Reconnecting',
    );

    act(() => source.emitOpen());
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent('Live');

    act(() => {
      source.emitError();
      source.emitError();
      source.emitError();
    });
    await waitFor(() => {
      expect(source.isClosed).toBe(true);
    });
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent(
      'Polling fallback',
    );

    const callsBefore = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore);
    });
  });

  it('transient offline within grace + online keeps EventSource (no polling)', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mockRunningFetch();
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());
    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent('Live');

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SSE_OFFLINE_FALLBACK_GRACE_MS - 500);
    });
    act(() => {
      window.dispatchEvent(new Event('online'));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SSE_OFFLINE_FALLBACK_GRACE_MS + 1000);
    });

    expect(source.isClosed).toBe(false);
    expect(screen.getByTestId('sse-connection-state')).toHaveTextContent('Live');
    expect(screen.queryByText('Polling fallback')).not.toBeInTheDocument();
  });

  it('sustained offline beyond grace on active execution switches to polling', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { fetchMock } = mockRunningFetch();
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());
    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent('Live');

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SSE_OFFLINE_FALLBACK_GRACE_MS);
    });

    await waitFor(() => {
      expect(source.isClosed).toBe(true);
    });
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent(
      'Polling fallback',
    );

    // After fallback, online must not auto-return to SSE; 4s REST polling continues.
    act(() => {
      window.dispatchEvent(new Event('online'));
    });
    const callsBefore = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore);
    });
    expect(screen.getByTestId('sse-connection-state')).toHaveTextContent(
      'Polling fallback',
    );
    // No new EventSource after fallback + online.
    expect(FakeEventSource.instances.filter((s) => !s.isClosed)).toHaveLength(0);
  });

  it('terminal execution sustained offline does not enter polling fallback', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mockRunningFetch({ detailStatus: 'SUCCEEDED', stepStatus: 'SUCCEEDED' });
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());
    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    expect(await screen.findByTestId('sse-connection-state')).toHaveTextContent('Live');

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SSE_OFFLINE_FALLBACK_GRACE_MS + 500);
    });

    expect(source.isClosed).toBe(false);
    expect(screen.getByTestId('sse-connection-state')).toHaveTextContent('Live');
    expect(screen.queryByText('Polling fallback')).not.toBeInTheDocument();
  });

  it('unmount clears offline watchdog timer and listeners', async () => {
    mockRunningFetch();
    const { unmount } = renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    unmount();
    expect(source.isClosed).toBe(true);

    // Advancing past grace after unmount must not throw or open polling.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SSE_OFFLINE_FALLBACK_GRACE_MS + 1000);
    });
    expect(FakeEventSource.instances.filter((s) => !s.isClosed)).toHaveLength(0);
  });

  it('terminal snapshot stops polling fallback', async () => {
    const { setDetailStatus, fetchMock } = mockRunningFetch({
      detailStatus: 'RUNNING',
    });
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => {
      source.emitError();
      source.emitError();
      source.emitError();
    });
    await waitFor(() => expect(source.isClosed).toBe(true));

    setDetailStatus('SUCCEEDED');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    await waitFor(() => {
      expect(screen.getByText('SUCCEEDED')).toBeInTheDocument();
    });

    const callsAtTerminal = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    // No further polling once terminal.
    expect(fetchMock.mock.calls.length).toBe(callsAtTerminal);
  });

  it('selected Step detail refreshes when snapshot updates', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let stepStatus = 'RUNNING';
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.endsWith(`/executions/${EXEC_ID}`)) {
        return Promise.resolve(jsonResponse(baseDetail('RUNNING')));
      }
      if (u.endsWith('/steps')) {
        return Promise.resolve(jsonResponse({ items: [baseStep(stepStatus)] }));
      }
      if (u.includes(`/steps/${STEP_ID}`)) {
        return Promise.resolve(
          jsonResponse({ ...baseStep(stepStatus), attempts: [] }),
        );
      }
      return Promise.resolve(jsonResponse({ error: { code: 'NOT_FOUND', message: 'x' } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());

    await user.click(screen.getByRole('button', { name: /^Steps$/i }));
    await user.click(screen.getByText('send'));
    await waitFor(() => {
      expect(screen.getAllByText(/Attempts/i).length).toBeGreaterThan(0);
    });
    const stepDetailCallsBefore = fetchMock.mock.calls.filter((c) =>
      String(c[0]).includes(`/steps/${STEP_ID}`),
    ).length;

    stepStatus = 'SUCCEEDED';
    act(() => {
      source.emitNamed(
        'execution.step.succeeded',
        envelope('execution.step.succeeded', 'SUCCEEDED'),
        '30',
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SNAPSHOT_REFRESH_DEBOUNCE_MS + 10);
    });
    await waitFor(() => {
      const stepDetailCalls = fetchMock.mock.calls.filter((c) =>
        String(c[0]).includes(`/steps/${STEP_ID}`),
      ).length;
      expect(stepDetailCalls).toBeGreaterThan(stepDetailCallsBefore);
    });
  });

  it('unmount closes EventSource', async () => {
    mockRunningFetch();
    const { unmount } = renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    unmount();
    expect(source.isClosed).toBe(true);
  });

  it('WAITING_INPUT SSE → snapshot refresh shows MrtrInputPanel', async () => {
    let detailStatus = 'RUNNING';
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const u = String(url);
      if (u.endsWith(`/executions/${EXEC_ID}`)) {
        return Promise.resolve(jsonResponse(baseDetail(detailStatus)));
      }
      if (u.endsWith('/steps')) {
        return Promise.resolve(jsonResponse({ items: [] }));
      }
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
                  city: { type: 'string', description: 'City from SSE path' },
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
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());

    detailStatus = 'WAITING_INPUT';
    act(() => {
      source.emitNamed(
        'execution.waiting_input',
        envelope('execution.waiting_input', 'WAITING_INPUT'),
        '40',
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SNAPSHOT_REFRESH_DEBOUNCE_MS + 10);
    });
    expect(await screen.findByText(/City from SSE path/i)).toBeInTheDocument();
  });

  it('CANCEL_REQUESTED refresh updates cancel UX', async () => {
    const { setDetailStatus } = mockRunningFetch({ detailStatus: 'RUNNING' });
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());

    setDetailStatus('CANCEL_REQUESTED');
    act(() => {
      source.emitNamed(
        'execution.cancel_requested',
        envelope('execution.cancel_requested', 'CANCEL_REQUESTED'),
        '50',
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SNAPSHOT_REFRESH_DEBOUNCE_MS + 10);
    });
    expect(
      await screen.findByText(/Cancel requested — 진행 중 Step 정리/i),
    ).toBeInTheDocument();
  });

  it('bounds timeline to max events and prevents duplicate ids', async () => {
    mockRunningFetch();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });
    await screen.findByText(EXEC_ID);
    const source = await waitForSse();
    act(() => source.emitOpen());

    act(() => {
      for (let i = 1; i <= 205; i += 1) {
        source.emitNamed(
          'execution.step.progress',
          envelope('execution.step.progress'),
          String(i),
        );
      }
    });

    await user.click(screen.getByRole('button', { name: /^Events$/i }));
    const rows = await screen.findAllByTestId('execution-event-row');
    expect(rows.length).toBe(200);
    expect(screen.getByText(/200 events/)).toBeInTheDocument();
  });
});
