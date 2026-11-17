import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import ExecutionDetail from '@/screens/work/ExecutionDetail';
import {
  executionStatusGuidance,
  labelSseConnectionState,
  unknownOutcomeGuidance,
} from '@/screens/work/executionDetailUx';
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

function detail(status: string) {
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
    cancel_requested_at: status === 'CANCEL_REQUESTED' ? '2026-10-07T12:00:05Z' : null,
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

function stubDetail(status: string, stepStatus = 'RUNNING') {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.endsWith(`/executions/${EXEC_ID}`)) {
        return Promise.resolve(jsonResponse(detail(status)));
      }
      if (url.endsWith('/steps')) {
        return Promise.resolve(jsonResponse({
          items: [{
            id: STEP_ID,
            execution_id: EXEC_ID,
            step_key: 'send',
            step_type: 'TOOL',
            parent_step_id: null,
            sequence_hint: 1,
            mcp_tool_version_id: null,
            iteration_no: null,
            status: stepStatus,
            attempt_count: 1,
            condition_result: null,
            ready_at: null,
            started_at: '2026-10-07T12:00:01Z',
            finished_at: null,
            error_code: null,
            error_category: null,
            duration_ms: null,
          }],
        }));
      }
      return Promise.resolve(new Response('Not found', { status: 404 }));
    }),
  );
}

describe('ExecutionDetail beginner UX helpers', () => {
  it('explains key statuses in Korean without inventing Domain values', () => {
    for (const status of [
      'WAITING_APPROVAL',
      'WAITING_INPUT',
      'FAILED',
      'TIMED_OUT',
      'PARTIALLY_SUCCEEDED',
      'CANCEL_REQUESTED',
    ] as const) {
      const guide = executionStatusGuidance(status);
      expect(guide?.meaning).toBeTruthy();
      expect(guide?.nextAction).toMatch(/다음|확인|기다리|제출|승인|오류|단계/);
    }
    const unknown = unknownOutcomeGuidance();
    expect(unknown.nextAction).toMatch(/외부/);
    expect(unknown.nextAction).toMatch(/자동 재시도/);
    expect(unknown.nextAction).toMatch(/재시도 버튼은 제공하지 않습니다|자동 재시도는 하지/);
    expect(unknown.nextAction).not.toMatch(/자동으로 재시도하세요|다시 시도하세요/);
    expect(labelSseConnectionState('live')).toBe('실시간');
    expect(labelSseConnectionState('polling')).toBe('폴링 대체');
  });
});

describe('ExecutionDetail beginner status banners', () => {
  beforeEach(() => {
    stubFakeEventSource();
    setCachedCsrfTokenForTests('csrf');
  });

  afterEach(() => {
    FakeEventSource.reset();
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('shows Korean tabs and WAITING_APPROVAL next action without new approve/retry CTAs', async () => {
    stubDetail('WAITING_APPROVAL', 'WAITING_APPROVAL');
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });

    expect(await screen.findByTestId('execution-status-guidance')).toBeInTheDocument();
    expect(screen.getByTestId('execution-status-next')).toHaveTextContent(/승인/);
    expect(screen.getByRole('button', { name: '기본 정보' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '단계' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '이벤트' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '입력 / 출력' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '감사 이력' })).toBeInTheDocument();
    expect(screen.getByText('실행 목록')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /재시도/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /승인하기/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /새 실행/ })).not.toBeInTheDocument();
  });

  it('keeps UNKNOWN_OUTCOME external-check guidance and no auto-retry CTA', async () => {
    stubDetail('FAILED', 'UNKNOWN_OUTCOME');
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: `/executions/${EXEC_ID}`,
    });

    expect(await screen.findByTestId('execution-unknown-outcome-alert')).toHaveTextContent(/외부/);
    expect(screen.getByTestId('execution-unknown-outcome-alert')).toHaveTextContent(/자동 재시도/);
    expect(screen.queryByRole('button', { name: /재시도/ })).not.toBeInTheDocument();
  });
});
