// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import ScheduleEdit from '@/screens/work/ScheduleEdit';
import Schedules from '@/screens/work/Schedules';
import { renderWithRouter } from '../test-utils';

const mocks = vi.hoisted(() => ({
  listAgents: vi.fn(), agentVersions: vi.fn(),
  workflows: vi.fn(), workflowVersions: vi.fn(),
  list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), status: vi.fn(), occurrences: vi.fn(),
}));

vi.mock('@/api/agents', () => ({ listAgents: mocks.listAgents, listAgentVersions: mocks.agentVersions }));
vi.mock('@/api/workflows', () => ({ listWorkflowTargets: mocks.workflows, listWorkflowTargetVersions: mocks.workflowVersions }));
vi.mock('@/api/schedules', () => ({
  listSchedules: mocks.list, getSchedule: mocks.get, createSchedule: mocks.create,
  updateSchedule: mocks.update, changeScheduleStatus: mocks.status,
  listScheduleOccurrences: mocks.occurrences,
}));

const agentId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const versionId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const scheduleId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const page = (items: unknown[]) => ({ items, page: 1, page_size: 100, total: items.length, has_next: false });

const existing = {
  id: scheduleId, name: '정기 예약', description: null, owner_id: agentId,
  target_type: 'AGENT_VERSION', target_id: versionId,
  schedule_type: 'ONCE', schedule_expression: '2026-12-10T09:00:00', timezone: 'Asia/Seoul',
  inputs: { request_text: '조회해 줘' }, overlap_policy: 'SKIP', misfire_policy: 'SKIP',
  max_catch_up: 1, status: 'ACTIVE', next_run_at: '2026-12-10T00:00:00Z', last_run_at: null,
  start_at: null, end_at: null, created_at: '2026-11-06T00:00:00Z',
  updated_at: '2026-11-06T00:00:00Z', lock_version: 4,
};
const firstOccurrence = {
  id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', schedule_id: scheduleId,
  scheduled_for: '2026-11-09T04:49:33Z', status: 'COMPLETED', decision_reason: 'DUE',
  execution_id: null, created_at: '2026-11-09T04:49:34Z', enqueued_at: null, finished_at: null,
};
const nextOccurrence = {
  ...firstOccurrence, id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
  scheduled_for: '2026-11-09T04:52:33Z', status: 'SKIPPED', decision_reason: 'MISFIRE_SKIP',
};

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.listAgents.mockResolvedValue(page([{ id: agentId, name: 'Pets Agent', status: 'ACTIVE', current_version_id: versionId }]));
  mocks.agentVersions.mockResolvedValue(page([{ id: versionId, version_no: 3, status: 'PUBLISHED', validation_status: 'VALID' }]));
  mocks.workflows.mockResolvedValue(page([]));
  mocks.workflowVersions.mockResolvedValue(page([]));
  mocks.list.mockResolvedValue(page([existing]));
  mocks.get.mockResolvedValue(existing);
  mocks.create.mockResolvedValue({ ...existing, status: 'PAUSED' });
  mocks.update.mockResolvedValue(existing);
  mocks.status.mockResolvedValue(existing);
  mocks.occurrences.mockResolvedValue(page([]));
});

afterEach(() => vi.restoreAllMocks());

describe('Schedule live management', () => {
  it('shows live API rows (not mock examples) and invokes pause', async () => {
    renderWithRouter(<Schedules />, { path: '/schedules', route: '/schedules' });
    expect(await screen.findByText('정기 예약')).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '일시정지' }));
    await waitFor(() => expect(mocks.status).toHaveBeenCalledWith(scheduleId, 'pause'));
  });

  it('quietly refreshes visible schedules every 15 seconds without requiring page reload', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    mocks.list.mockResolvedValueOnce(page([{ ...existing, status: 'ACTIVE' }]))
      .mockResolvedValueOnce(page([{ ...existing, name: '자동 갱신됨', status: 'PAUSED' }]));
    renderWithRouter(<Schedules />, { path: '/schedules', route: '/schedules' });
    expect(await screen.findByText('정기 예약')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('15초마다 자동 갱신');
    const tick = intervalSpy.mock.calls.find(([, delay]) => delay === 15_000)?.[0];
    expect(tick).toBeTypeOf('function');
    await act(async () => { (tick as () => void)(); });
    expect(await screen.findByText('자동 갱신됨')).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalledTimes(2);
  });

  it('does not poll in a background tab, but refreshes upon returning', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    const visibilitySpy = vi.spyOn(document, 'visibilityState', 'get');
    renderWithRouter(<Schedules />, { path: '/schedules', route: '/schedules' });
    expect(await screen.findByText('정기 예약')).toBeInTheDocument();
    const tick = intervalSpy.mock.calls.find(([, delay]) => delay === 15_000)?.[0];
    expect(tick).toBeTypeOf('function');
    visibilitySpy.mockReturnValue('hidden');
    await act(async () => { (tick as () => void)(); });
    expect(mocks.list).toHaveBeenCalledTimes(1);
    visibilitySpy.mockReturnValue('visible');
    await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
  });

  it('keeps displayed rows when a background refresh fails', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    mocks.list.mockResolvedValueOnce(page([existing])).mockRejectedValueOnce(new Error('network'));
    renderWithRouter(<Schedules />, { path: '/schedules', route: '/schedules' });
    expect(await screen.findByText('정기 예약')).toBeInTheDocument();
    const tick = intervalSpy.mock.calls.find(([, delay]) => delay === 15_000)?.[0];
    await act(async () => { (tick as () => void)(); });
    expect(screen.getByText('정기 예약')).toBeInTheDocument();
    expect(screen.getByText(/자동 갱신에 실패했습니다/)).toBeInTheDocument();
  });

  it('refreshes only detail occurrence history every 15 seconds without resetting unsaved fields', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    mocks.get.mockResolvedValueOnce({ ...existing, status: 'PAUSED' });
    mocks.occurrences.mockResolvedValueOnce(page([firstOccurrence])).mockResolvedValueOnce(page([nextOccurrence, firstOccurrence]));
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    expect(await screen.findByText(/13:49.*COMPLETED.*DUE/)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('15초마다 자동 갱신');
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '저장 전 수정한 이름' } });
    const tick = intervalSpy.mock.calls.find(([, delay]) => delay === 15_000)?.[0];
    expect(tick).toBeTypeOf('function');
    await act(async () => { (tick as () => void)(); });
    expect(await screen.findByText(/13:52.*SKIPPED.*MISFIRE_SKIP/)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '이름' })).toHaveValue('저장 전 수정한 이름');
    expect(mocks.get).toHaveBeenCalledTimes(1);
    expect(mocks.occurrences).toHaveBeenCalledTimes(2);
    expect(mocks.occurrences).toHaveBeenLastCalledWith(scheduleId, expect.objectContaining({ page_size: 20 }));
  });

  it('skips detail history polling while hidden and refreshes on tab return', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    const visibilitySpy = vi.spyOn(document, 'visibilityState', 'get');
    mocks.occurrences.mockResolvedValueOnce(page([firstOccurrence])).mockResolvedValueOnce(page([nextOccurrence]));
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    expect(await screen.findByText(/13:49.*COMPLETED.*DUE/)).toBeInTheDocument();
    const tick = intervalSpy.mock.calls.find(([, delay]) => delay === 15_000)?.[0];
    visibilitySpy.mockReturnValue('hidden');
    await act(async () => { (tick as () => void)(); });
    expect(mocks.occurrences).toHaveBeenCalledTimes(1);
    visibilitySpy.mockReturnValue('visible');
    await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    expect(await screen.findByText(/13:52.*SKIPPED.*MISFIRE_SKIP/)).toBeInTheDocument();
    expect(mocks.occurrences).toHaveBeenCalledTimes(2);
  });

  it('preserves previous detail history on polling failure and recovers by manual history refresh', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    mocks.occurrences.mockResolvedValueOnce(page([firstOccurrence]))
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce(page([nextOccurrence]));
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    expect(await screen.findByText(/13:49.*COMPLETED.*DUE/)).toBeInTheDocument();
    const tick = intervalSpy.mock.calls.find(([, delay]) => delay === 15_000)?.[0];
    await act(async () => { (tick as () => void)(); });
    expect(screen.getByText(/13:49.*COMPLETED.*DUE/)).toBeInTheDocument();
    expect(screen.getByText(/이력 자동 갱신에 실패했습니다/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '이력 새로고침' }));
    expect(await screen.findByText(/13:52.*SKIPPED.*MISFIRE_SKIP/)).toBeInTheDocument();
    expect(screen.queryByText(/이력 자동 갱신에 실패했습니다/)).not.toBeInTheDocument();
    expect(mocks.occurrences).toHaveBeenCalledTimes(3);
    expect(mocks.get).toHaveBeenCalledTimes(1);
  });

  it('stops detail history polling and visibility listeners when leaving the page', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    const clearIntervalSpy = vi.spyOn(window, 'clearInterval');
    const { unmount } = renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    await waitFor(() => expect(mocks.occurrences).toHaveBeenCalledTimes(1));
    const timer = intervalSpy.mock.results.find((_, index) => intervalSpy.mock.calls[index][1] === 15_000)?.value;
    unmount();
    expect(clearIntervalSpy).toHaveBeenCalledWith(timer);
    fireEvent(document, new Event('visibilitychange'));
    expect(mocks.occurrences).toHaveBeenCalledTimes(1);
  });

  it('offers published Agent versions, recurring schedules and canonical policies', async () => {
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/new', route: '/schedules/new' });
    expect(screen.getByText('Agent Version')).toBeInTheDocument();
    expect(screen.getByText('Workflow Version')).toBeInTheDocument();
    expect(screen.getByText(/일시정지\(PAUSED\) 상태로 저장/)).toBeInTheDocument();
    expect(screen.queryByText('ACTIVE')).not.toBeInTheDocument();

    await waitFor(() => expect(within(screen.getByRole('combobox', { name: '대상 선택' })).getByRole('option', { name: 'Pets Agent' })).toBeInTheDocument());
    fireEvent.change(screen.getByRole('combobox', { name: '대상 선택' }), { target: { value: agentId } });
    await waitFor(() => expect(within(screen.getByRole('combobox', { name: '게시된 Version' })).getByRole('option', { name: /v3/ })).toBeInTheDocument());
    expect(within(screen.getByRole('combobox', { name: '예약 종류' })).getByRole('option', { name: /CRON/ })).toBeInTheDocument();
    expect(within(screen.getByRole('combobox', { name: '예약 종류' })).getByRole('option', { name: /INTERVAL/ })).toBeInTheDocument();
    const overlap = screen.getByRole('combobox', { name: '동시 실행 정책' });
    const misfire = screen.getByRole('combobox', { name: '미실행 처리 정책' });
    expect(within(overlap).getByRole('option', { name: 'REPLACE' })).toBeInTheDocument();
    expect(within(misfire).getByRole('option', { name: 'CATCH_UP_LIMITED' })).toBeInTheDocument();
  });

  it('creates an actual PAUSED Agent Schedule with the chosen Version ID', async () => {
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/new', route: '/schedules/new' });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '대상 선택' })).toBeEnabled());
    fireEvent.change(screen.getByRole('combobox', { name: '대상 선택' }), { target: { value: agentId } });
    await waitFor(() => expect(within(screen.getByRole('combobox', { name: '게시된 Version' })).getByRole('option', { name: /v3/ })).toBeInTheDocument());
    fireEvent.change(screen.getByRole('combobox', { name: '게시된 Version' }), { target: { value: versionId } });
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '내 예약' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Agent 업무 지시' }), { target: { value: '반려동물 목록' } });
    fireEvent.change(screen.getByLabelText('실행 시각'), { target: { value: '2026-12-10T09:00' } });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalled());
    expect(mocks.create.mock.calls[0][0]).toMatchObject({
      name: '내 예약', target_type: 'AGENT_VERSION', target_id: versionId,
      schedule_type: 'ONCE', schedule_expression: '2026-12-10T09:00:00',
      inputs: { request_text: '반려동물 목록' },
    });
    expect(mocks.create.mock.calls[0][0]).not.toHaveProperty('status');
  });

  it('creates a daily CRON Agent Schedule with a pinned version and task', async () => {
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/new', route: '/schedules/new' });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '대상 선택' })).toBeEnabled());
    fireEvent.change(screen.getByRole('combobox', { name: '대상 선택' }), { target: { value: agentId } });
    await waitFor(() => expect(within(screen.getByRole('combobox', { name: '게시된 Version' })).getByRole('option', { name: /v3/ })).toBeInTheDocument());
    fireEvent.change(screen.getByRole('combobox', { name: '게시된 Version' }), { target: { value: versionId } });
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '아침 조회' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Agent 업무 지시' }), { target: { value: '오늘 반려동물 목록을 조회해 줘' } });
    fireEvent.change(screen.getByRole('combobox', { name: '예약 종류' }), { target: { value: 'CRON' } });
    expect(screen.getByRole('combobox', { name: '반복 규칙' })).toHaveValue('DAILY');
    expect(screen.getByLabelText('반복 실행 시간')).toHaveValue('09:00');
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({
      name: '아침 조회', target_type: 'AGENT_VERSION', target_id: versionId,
      schedule_type: 'CRON', schedule_expression: '0 9 * * *', timezone: 'Asia/Seoul',
      inputs: { request_text: '오늘 반려동물 목록을 조회해 줘' },
      overlap_policy: 'SKIP', misfire_policy: 'SKIP', max_catch_up: 1,
    })));
  });

  it('creates a weekly CRON expression using weekday and local time controls', async () => {
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/new', route: '/schedules/new' });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '대상 선택' })).toBeEnabled());
    fireEvent.change(screen.getByRole('combobox', { name: '대상 선택' }), { target: { value: agentId } });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '게시된 Version' })).toBeEnabled());
    fireEvent.change(screen.getByRole('combobox', { name: '게시된 Version' }), { target: { value: versionId } });
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '금요일 업무' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Agent 업무 지시' }), { target: { value: '조회' } });
    fireEvent.change(screen.getByRole('combobox', { name: '예약 종류' }), { target: { value: 'CRON' } });
    fireEvent.change(screen.getByRole('combobox', { name: '반복 규칙' }), { target: { value: 'WEEKLY' } });
    fireEvent.change(screen.getByLabelText('반복 실행 시간'), { target: { value: '14:30' } });
    fireEvent.change(screen.getByRole('combobox', { name: '반복 요일' }), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({
      schedule_type: 'CRON', schedule_expression: '30 14 * * 5',
    })));
  });

  it('creates three-minute intervals from number and unit controls', async () => {
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/new', route: '/schedules/new' });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '대상 선택' })).toBeEnabled());
    fireEvent.change(screen.getByRole('combobox', { name: '대상 선택' }), { target: { value: agentId } });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '게시된 Version' })).toBeEnabled());
    fireEvent.change(screen.getByRole('combobox', { name: '게시된 Version' }), { target: { value: versionId } });
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '3분 간격 조회' } });
    fireEvent.change(screen.getByRole('textbox', { name: 'Agent 업무 지시' }), { target: { value: '조회' } });
    fireEvent.change(screen.getByRole('combobox', { name: '예약 종류' }), { target: { value: 'INTERVAL' } });
    fireEvent.change(screen.getByLabelText('반복 간격 숫자'), { target: { value: '3' } });
    fireEvent.change(screen.getByRole('combobox', { name: '반복 간격 단위' }), { target: { value: 'MINUTES' } });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({
      schedule_type: 'INTERVAL', schedule_expression: 'PT3M',
    })));
  });

  it('locks configuration on ACTIVE and sends metadata-only PATCH with lock version', async () => {
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    await waitFor(() => expect(screen.getByRole('textbox', { name: '이름' })).toHaveValue('정기 예약'));
    expect(screen.getByRole('combobox', { name: '예약 종류' })).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '바뀐 이름' } });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith(scheduleId, { name: '바뀐 이름', description: null }, 4));
  });

  it('does not reset the preserved next_run_at on PAUSED metadata edits', async () => {
    mocks.get.mockResolvedValueOnce({ ...existing, status: 'PAUSED' });
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    await waitFor(() => expect(screen.getByRole('textbox', { name: '이름' })).toHaveValue('정기 예약'));
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '설명만 수정한 예약' } });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith(scheduleId, { name: '설명만 수정한 예약' }, 4));
  });

  it('preserves a paused three-minute expression during a metadata-only edit', async () => {
    mocks.get.mockResolvedValueOnce({ ...existing, status: 'PAUSED', schedule_type: 'INTERVAL', schedule_expression: 'PT3M' });
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    await waitFor(() => expect(screen.getByLabelText('반복 간격 숫자')).toHaveValue(3));
    expect(screen.getByRole('combobox', { name: '반복 간격 단위' })).toHaveValue('MINUTES');
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '이름만 수정' } });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith(scheduleId, { name: '이름만 수정' }, 4));
  });

  it('round-trips advanced simple intervals and protects complex intervals from accidental replacement', async () => {
    mocks.get.mockResolvedValueOnce({ ...existing, status: 'PAUSED', schedule_type: 'INTERVAL', schedule_expression: 'PT3M' });
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    await waitFor(() => expect(screen.getByLabelText('반복 간격 숫자')).toHaveValue(3));
    fireEvent.click(screen.getByRole('checkbox', { name: '고급 입력 (ISO-8601)' }));
    fireEvent.change(screen.getByRole('textbox', { name: '실행 간격 (ISO-8601)' }), { target: { value: 'PT15M' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '고급 입력 (ISO-8601)' }));
    expect(screen.getByLabelText('반복 간격 숫자')).toHaveValue(15);
    expect(screen.getByRole('combobox', { name: '반복 간격 단위' })).toHaveValue('MINUTES');
    fireEvent.click(screen.getByRole('checkbox', { name: '고급 입력 (ISO-8601)' }));
    fireEvent.change(screen.getByRole('textbox', { name: '실행 간격 (ISO-8601)' }), { target: { value: 'P1DT2H' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '고급 입력 (ISO-8601)' }));
    expect(screen.getByRole('checkbox', { name: '고급 입력 (ISO-8601)' })).toBeChecked();
    expect(screen.getByRole('textbox', { name: '실행 간격 (ISO-8601)' })).toHaveValue('P1DT2H');
    expect(screen.getByText(/현재 복합 실행 간격은 숫자·단위 입력으로 표현할 수 없습니다/)).toBeInTheDocument();
  });

  it('preserves an advanced CRON expression during a metadata-only edit', async () => {
    mocks.get.mockResolvedValueOnce({ ...existing, status: 'PAUSED', schedule_type: 'CRON', schedule_expression: '*/15 9-18 * * 1-5' });
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/:id/edit', route: `/schedules/${scheduleId}/edit` });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '반복 규칙' })).toHaveValue('ADVANCED'));
    expect(screen.getByRole('textbox', { name: 'CRON 표현식' })).toHaveValue('*/15 9-18 * * 1-5');
    fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: '이름만 수정' } });
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith(scheduleId, { name: '이름만 수정' }, 4));
  });
});
