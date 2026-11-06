// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
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

describe('Schedule live management', () => {
  it('shows live API rows (not mock examples) and invokes pause', async () => {
    renderWithRouter(<Schedules />, { path: '/schedules', route: '/schedules' });
    expect(await screen.findByText('정기 예약')).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '일시정지' }));
    await waitFor(() => expect(mocks.status).toHaveBeenCalledWith(scheduleId, 'pause'));
  });

  it('only offers explicit published Agent versions, canonical policies and ONCE', async () => {
    renderWithRouter(<ScheduleEdit />, { path: '/schedules/new', route: '/schedules/new' });
    expect(screen.getByText('Agent Version')).toBeInTheDocument();
    expect(screen.getByText('Workflow Version')).toBeInTheDocument();
    expect(screen.getByText(/일시정지\(PAUSED\) 상태로 저장/)).toBeInTheDocument();
    expect(screen.queryByText('ACTIVE')).not.toBeInTheDocument();

    await waitFor(() => expect(within(screen.getByRole('combobox', { name: '대상 선택' })).getByRole('option', { name: 'Pets Agent' })).toBeInTheDocument());
    fireEvent.change(screen.getByRole('combobox', { name: '대상 선택' }), { target: { value: agentId } });
    await waitFor(() => expect(within(screen.getByRole('combobox', { name: '게시된 Version' })).getByRole('option', { name: /v3/ })).toBeInTheDocument());
    expect(within(screen.getByRole('combobox', { name: '예약 종류' })).queryByRole('option', { name: /CRON/ })).not.toBeInTheDocument();
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
});
