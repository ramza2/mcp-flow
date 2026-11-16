// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Approvals from '@/screens/work/Approvals';
import ApprovalDetail from '@/screens/work/ApprovalDetail';
import { ApiError } from '@/api/client';
import { renderWithRouter } from '../test-utils';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
}));

vi.mock('@/api/approvals', () => ({
  listApprovals: mocks.list,
  getApproval: mocks.get,
}));

const approvalId = '11111111-1111-4111-8111-111111111111';
const executionId = '22222222-2222-4222-8222-222222222222';
const stepId = '33333333-3333-4333-8333-333333333333';
const requesterId = '44444444-4444-4444-8444-444444444444';

const listItem = {
  id: approvalId,
  status: 'PENDING',
  execution_id: executionId,
  step_execution_id: stepId,
  requested_by: requesterId,
  decision_mode: 'ANY',
  required_approvals: 1,
  requested_at: '2026-11-16T00:00:00.000Z',
  expires_at: '2026-11-16T01:00:00.000Z',
  resolved_at: null,
  approve_count: 0,
  reject_count: 0,
  can_decide: true,
};

const detail = {
  ...listItem,
  safe_context: {
    tool_name: 'deletePet',
    risk_class: 'DESTRUCTIVE',
    arguments: { petId: { kind: 'SECRET_REF', masked: true } },
  },
  decisions: [
    {
      decision_id: '55555555-5555-4555-8555-555555555555',
      decided_by: '66666666-6666-4666-8666-666666666666',
      decision: 'APPROVE',
      comment: '확인 완료',
      decided_at: '2026-11-16T00:30:00.000Z',
    },
  ],
};

beforeEach(() => {
  mocks.list.mockReset();
  mocks.get.mockReset();
  mocks.list.mockResolvedValue({
    items: [listItem],
    page: 1,
    page_size: 20,
    total: 1,
    has_next: false,
  });
  mocks.get.mockResolvedValue(detail);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Approvals live list (phase 1 read)', () => {
  it('loads GET /approvals PENDING inbox and shows can_decide without mock purpose/tool fields', async () => {
    renderWithRouter(<Approvals />, { path: '/approvals', route: '/approvals' });

    expect(await screen.findByText('가능')).toBeInTheDocument();
    const pendingTitles = screen.getAllByTitle('PENDING');
    expect(pendingTitles.length).toBeGreaterThan(0);
    expect(pendingTitles.some((el) => el.textContent?.includes('대기'))).toBe(true);
    expect(screen.getByText(/아무나 1명/)).toBeInTheDocument();
    expect(screen.queryByText('WAITING_APPROVAL')).not.toBeInTheDocument();
    expect(screen.queryByText(/pets\.delete|원본 요청/i)).not.toBeInTheDocument();

    await waitFor(() => {
      expect(mocks.list).toHaveBeenCalled();
      const args = mocks.list.mock.calls[0][0] as { status?: string; page?: number };
      expect(args.status).toBe('PENDING');
      expect(args.page).toBe(1);
    });
  });

  it('shows empty Korean guidance for an empty PENDING inbox', async () => {
    mocks.list.mockResolvedValue({
      items: [],
      page: 1,
      page_size: 20,
      total: 0,
      has_next: false,
    });
    renderWithRouter(<Approvals />, { path: '/approvals', route: '/approvals' });
    expect(await screen.findByText('대기 중인 승인 요청이 없습니다.')).toBeInTheDocument();
  });

  it('shows permission denied on 403', async () => {
    mocks.list.mockRejectedValue(
      new ApiError({
        status: 403,
        code: 'AUTH_FORBIDDEN',
        message: 'Missing approval.decide permission.',
      }),
    );
    renderWithRouter(<Approvals />, { path: '/approvals', route: '/approvals' });
    expect(await screen.findByText('접근 권한이 없습니다')).toBeInTheDocument();
    expect(screen.getByText(/승인 결정 권한/)).toBeInTheDocument();
  });

  it('shows API error with retry on non-auth failures', async () => {
    mocks.list.mockRejectedValue(
      new ApiError({
        status: 500,
        code: 'INTERNAL',
        message: '일시 오류',
        requestId: 'req-approval-1',
      }),
    );
    renderWithRouter(<Approvals />, { path: '/approvals', route: '/approvals' });
    expect(await screen.findByText('일시 오류')).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-approval-1/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '다시 시도' })).toBeInTheDocument();
  });
});

describe('ApprovalDetail live read (phase 1)', () => {
  it('shows status, can_decide, safe_context, decision history — no approve/reject POST UI', async () => {
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });

    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();
    const pendingBadges = screen.getAllByText('대기');
    expect(pendingBadges[0]).toHaveAttribute('title', 'PENDING');
    expect(screen.queryByText('WAITING_APPROVAL')).not.toBeInTheDocument();

    expect(screen.getByText(/safe_context/)).toBeInTheDocument();
    expect(screen.getByText(/deletePet/)).toBeInTheDocument();
    expect(screen.getByText(/masked/)).toBeInTheDocument();
    expect(screen.queryByText('context_snapshot')).not.toBeInTheDocument();

    expect(screen.getByText('결정 이력')).toBeInTheDocument();
    expect(screen.getByText('승인')).toBeInTheDocument();
    expect(screen.getByText('확인 완료')).toBeInTheDocument();

    expect(screen.queryByRole('button', { name: '승인' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '거절' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '승인하기' })).not.toBeInTheDocument();
    expect(screen.getByText(/조회만 지원/)).toBeInTheDocument();

    await waitFor(() => {
      expect(mocks.get).toHaveBeenCalledWith(approvalId, expect.any(AbortSignal));
    });
  });

  it('shows 404 Korean empty state (not 403) when detail is not visible', async () => {
    mocks.get.mockRejectedValue(
      new ApiError({ status: 404, code: 'NOT_FOUND', message: 'Not found' }),
    );
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('승인 요청을 찾을 수 없습니다')).toBeInTheDocument();
    expect(screen.getByText(/404/)).toBeInTheDocument();
  });

  it('shows 403 permission denied for global auth failure', async () => {
    mocks.get.mockRejectedValue(
      new ApiError({
        status: 403,
        code: 'AUTH_FORBIDDEN',
        message: 'Missing approval.decide permission.',
      }),
    );
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('접근 권한이 없습니다')).toBeInTheDocument();
  });

  it('never remaps Approval PENDING to WAITING_APPROVAL in badges', async () => {
    mocks.get.mockResolvedValue({ ...detail, decisions: [], can_decide: false });
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로는 결정할 수 없음')).toBeInTheDocument();
    for (const badge of screen.getAllByText('대기')) {
      expect(badge).toHaveAttribute('title', 'PENDING');
    }
    expect(screen.queryByText('WAITING_APPROVAL')).not.toBeInTheDocument();
  });
});

describe('Approvals status filter', () => {
  it('requests non-PENDING status when filter changes', async () => {
    const user = userEvent.setup();
    renderWithRouter(<Approvals />, { path: '/approvals', route: '/approvals' });
    await screen.findByText('가능');

    const statusSelect = screen.getByRole('combobox', { name: '상태' });
    await user.selectOptions(statusSelect, 'APPROVED');

    await waitFor(() => {
      const last = mocks.list.mock.calls.at(-1)?.[0] as { status?: string };
      expect(last.status).toBe('APPROVED');
    });
    expect(
      within(statusSelect).getByRole('option', { name: '승인됨' }),
    ).toHaveValue('APPROVED');
  });
});
