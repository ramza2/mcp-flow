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
  decide: vi.fn(),
}));

vi.mock('@/api/approvals', () => ({
  listApprovals: mocks.list,
  getApproval: mocks.get,
  createApprovalDecision: mocks.decide,
}));

const approvalId = '11111111-1111-4111-8111-111111111111';
const executionId = '22222222-2222-4222-8222-222222222222';
const stepId = '33333333-3333-4333-8333-333333333333';
const requesterId = '44444444-4444-4444-8444-444444444444';
const decisionId = '77777777-7777-4777-8777-777777777777';

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
  decisions: [] as Array<{
    decision_id: string;
    decided_by: string;
    decision: string;
    comment: string | null;
    decided_at: string;
  }>,
};

const approveResult = {
  decision_id: decisionId,
  approval_request_id: approvalId,
  approval_status: 'APPROVED',
  execution_id: executionId,
  step_execution_id: stepId,
  execution_status: 'WAITING_APPROVAL',
  step_status: 'WAITING_APPROVAL',
  decision: 'APPROVE',
  decided_by: requesterId,
  decided_at: '2026-11-16T00:30:00.000Z',
  resume_enqueued: true,
};

beforeEach(() => {
  mocks.list.mockReset();
  mocks.get.mockReset();
  mocks.decide.mockReset();
  mocks.list.mockResolvedValue({
    items: [listItem],
    page: 1,
    page_size: 20,
    total: 1,
    has_next: false,
  });
  mocks.get.mockResolvedValue(detail);
  mocks.decide.mockResolvedValue(approveResult);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Approvals live list', () => {
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

describe('ApprovalDetail decide (phase 2)', () => {
  it('shows approve/reject only when PENDING && can_decide', async () => {
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });

    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '승인' }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: '거절' }).length).toBeGreaterThan(0);
    expect(screen.queryByText(/조회만 지원/)).not.toBeInTheDocument();
  });

  it('hides decide buttons when can_decide is false', async () => {
    mocks.get.mockResolvedValue({ ...detail, can_decide: false });
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로는 결정할 수 없음')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '승인' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '거절' })).not.toBeInTheDocument();
  });

  it('approves via confirmation, shows server result, and refreshes detail GET', async () => {
    const user = userEvent.setup();
    mocks.get
      .mockResolvedValueOnce(detail)
      .mockResolvedValueOnce({
        ...detail,
        status: 'APPROVED',
        can_decide: false,
        approve_count: 1,
        resolved_at: '2026-11-16T00:30:00.000Z',
        decisions: [
          {
            decision_id: decisionId,
            decided_by: requesterId,
            decision: 'APPROVE',
            comment: null,
            decided_at: '2026-11-16T00:30:00.000Z',
          },
        ],
      });

    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();

    await user.click(screen.getAllByRole('button', { name: '승인' })[0]);
    expect(screen.getByText('승인 확인')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '승인 확정' }));

    await waitFor(() => {
      expect(mocks.decide).toHaveBeenCalledWith(approvalId, {
        decision: 'APPROVE',
        comment: null,
      });
    });
    expect(await screen.findByText('승인 결정이 저장되었습니다')).toBeInTheDocument();
    expect(screen.getAllByText('WAITING_APPROVAL').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/EXECUTION_APPROVAL_RESUME/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '승인' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '거절' })).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.get.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('hides decide buttons while quiet GET refresh is still pending after success', async () => {
    const user = userEvent.setup();
    let resolveRefresh: (value: unknown) => void = () => undefined;
    mocks.get
      .mockResolvedValueOnce(detail)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRefresh = resolve;
          }),
      );

    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: '승인' })[0]);
    await user.click(screen.getByRole('button', { name: '승인 확정' }));

    expect(await screen.findByText('승인 결정이 저장되었습니다')).toBeInTheDocument();
    // Stale detail is still PENDING/can_decide, but decide UI must stay hidden.
    expect(screen.queryByRole('button', { name: '승인' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '거절' })).not.toBeInTheDocument();
    expect(screen.queryByText(/승인 \/ 거절/)).not.toBeInTheDocument();

    resolveRefresh({
      ...detail,
      status: 'APPROVED',
      can_decide: false,
      approve_count: 1,
      decisions: [
        {
          decision_id: decisionId,
          decided_by: requesterId,
          decision: 'APPROVE',
          comment: null,
          decided_at: '2026-11-16T00:30:00.000Z',
        },
      ],
    });
    await waitFor(() => expect(mocks.get.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(screen.queryByRole('button', { name: '승인' })).not.toBeInTheDocument();
  });

  it('keeps decide buttons hidden when quiet GET refresh fails after success', async () => {
    const user = userEvent.setup();
    mocks.get
      .mockResolvedValueOnce(detail)
      .mockRejectedValueOnce(
        new ApiError({
          status: 500,
          code: 'INTERNAL',
          message: 'refresh failed',
        }),
      );

    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: '승인' })[0]);
    await user.click(screen.getByRole('button', { name: '승인 확정' }));

    expect(await screen.findByText('승인 결정이 저장되었습니다')).toBeInTheDocument();
    expect(
      await screen.findByText(/상세를 다시 불러오지 못했습니다/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '승인' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '거절' })).not.toBeInTheDocument();
  });

  it('rejects with comment and does not claim success on 409 conflict', async () => {
    const user = userEvent.setup();
    mocks.decide.mockRejectedValue(
      new ApiError({
        status: 409,
        code: 'RESOURCE_CONFLICT',
        message: 'Already decided.',
        requestId: 'req-conflict',
      }),
    );
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();

    await user.click(screen.getAllByRole('button', { name: '거절' })[0]);
    await user.type(screen.getByPlaceholderText(/거절 사유/), '정책상 거절');
    await user.click(screen.getByRole('button', { name: '거절 확정' }));

    await waitFor(() => {
      expect(mocks.decide).toHaveBeenCalledWith(approvalId, {
        decision: 'REJECT',
        comment: '정책상 거절',
      });
    });
    expect(await screen.findByText(/Already decided/)).toBeInTheDocument();
    expect(screen.queryByText(/결정이 저장되었습니다/)).not.toBeInTheDocument();
  });

  it('does not claim success on network uncertainty', async () => {
    const user = userEvent.setup();
    mocks.decide.mockRejectedValue(
      new ApiError({
        status: 0,
        code: 'NETWORK_ERROR',
        message: 'Network request failed',
      }),
    );
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: '승인' })[0]);
    await user.click(screen.getByRole('button', { name: '승인 확정' }));
    expect(await screen.findByText(/성공으로 표시하지 않습니다/)).toBeInTheDocument();
    expect(screen.queryByText(/결정이 저장되었습니다/)).not.toBeInTheDocument();
  });

  it('prevents double submit while a decide request is in flight', async () => {
    const user = userEvent.setup();
    let resolveDecide: (value: unknown) => void = () => undefined;
    mocks.decide.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveDecide = resolve;
        }),
    );
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: '승인' })[0]);
    await user.click(screen.getByRole('button', { name: '승인 확정' }));
    await waitFor(() => expect(mocks.decide).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole('button', { name: '승인 확정' }));
    expect(mocks.decide).toHaveBeenCalledTimes(1);
    resolveDecide(approveResult);
    expect(await screen.findByText('승인 결정이 저장되었습니다')).toBeInTheDocument();
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
