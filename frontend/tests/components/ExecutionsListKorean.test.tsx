// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Executions from '@/screens/work/Executions';
import { labelExecutionSource, labelExecutionStatus } from '@/domain';
import { renderWithRouter } from '../test-utils';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const EXEC_ID = '11111111-1111-4111-8111-111111111111';

const item = {
  id: EXEC_ID,
  status: 'RUNNING',
  source_type: 'AGENT_REQUEST',
  source: { type: 'AGENT_REQUEST', id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', name: 'Ops Agent' },
  requester_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  step_count: 2,
  completed_step_count: 1,
  duration_ms: 1200,
  requested_at: '2026-11-18T00:00:00.000Z',
  started_at: '2026-11-18T00:00:01.000Z',
  finished_at: null,
  error_code: null,
  error_category: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Executions list Korean UX', () => {
  it('shows Korean title, columns, filters, sort labels; keeps canonical filter values', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ items: [item], page: 1, page_size: 20, total: 1 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    renderWithRouter(<Executions />, { path: '/executions', route: '/executions' });

    expect(await screen.findByRole('heading', { name: '실행 이력' })).toBeInTheDocument();
    expect(screen.getByText('실행 ID')).toBeInTheDocument();
    expect(screen.getByText('출처')).toBeInTheDocument();
    expect(screen.getByText('요청자')).toBeInTheDocument();
    expect(screen.getByText('단계')).toBeInTheDocument();
    expect(screen.getByText('소요 시간')).toBeInTheDocument();
    expect(screen.getByText('요청 시각')).toBeInTheDocument();
    expect(screen.getByText('시작 시각')).toBeInTheDocument();
    expect(screen.getAllByText(labelExecutionSource('AGENT_REQUEST')).length).toBeGreaterThan(0);
    const runningBadges = screen.getAllByTitle('RUNNING');
    expect(runningBadges.length).toBeGreaterThan(0);
    expect(runningBadges.some((el) => el.textContent?.includes(labelExecutionStatus('RUNNING')))).toBe(
      true,
    );

    expect(screen.queryByRole('heading', { name: 'Executions' })).not.toBeInTheDocument();
    expect(screen.queryByText('Execution ID')).not.toBeInTheDocument();
    expect(screen.queryByText('Requester')).not.toBeInTheDocument();
    expect(screen.queryByText('Duration')).not.toBeInTheDocument();

    const statusSelect = screen.getByRole('combobox', { name: '상태' });
    expect(
      within(statusSelect).getByRole('option', { name: labelExecutionStatus('FAILED') }),
    ).toHaveValue('FAILED');

    const sourceSelect = screen.getByRole('combobox', { name: '출처' });
    expect(
      within(sourceSelect).getByRole('option', {
        name: labelExecutionSource('WORKFLOW_VERSION'),
      }),
    ).toHaveValue('WORKFLOW_VERSION');

    const sortSelect = screen.getByRole('combobox', { name: '정렬' });
    expect(within(sortSelect).getByRole('option', { name: '요청 최신 순' })).toHaveValue(
      '-requested_at',
    );
    expect(within(sortSelect).getByRole('option', { name: '상태 오름차순' })).toHaveValue(
      'status',
    );

    await user.selectOptions(statusSelect, 'FAILED');
    await waitFor(() => {
      expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('status=FAILED');
    });
  });

  it('shows Korean empty copy without English Execution wording', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ items: [], page: 1, page_size: 20, total: 0 })),
    );
    renderWithRouter(<Executions />, { path: '/executions', route: '/executions' });
    expect(await screen.findByText('실행이 없습니다.')).toBeInTheDocument();
    expect(screen.getByText(/에이전트·워크플로·예약 실행/)).toBeInTheDocument();
    expect(screen.queryByText(/Execution ID|Executions\b/)).not.toBeInTheDocument();
  });
});
