// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import Approvals from '@/screens/work/Approvals';
import ApprovalDetail from '@/screens/work/ApprovalDetail';
import { ApiError } from '@/api/client';
import { renderWithRouter } from '../test-utils';
import {
  APPROVAL_REVIEW_FLOW_LABEL,
  approvalStatusGuidance,
  approvalVsExecutionNote,
  approvalsListEmptyGuidance,
  formatApprovalDecisionError,
} from '@/screens/work/approvalUx';

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
const detail = {
  id: approvalId,
  status: 'PENDING',
  execution_id: '22222222-2222-4222-8222-222222222222',
  step_execution_id: '33333333-3333-4333-8333-333333333333',
  requested_by: '44444444-4444-4444-8444-444444444444',
  decision_mode: 'ANY',
  required_approvals: 1,
  requested_at: '2026-11-16T00:00:00.000Z',
  expires_at: '2026-11-16T01:00:00.000Z',
  resolved_at: null,
  approve_count: 0,
  reject_count: 0,
  can_decide: true,
  safe_context: { risk_class: 'DESTRUCTIVE', tool_name: 'deletePet' },
  decisions: [],
};

beforeEach(() => {
  mocks.list.mockReset();
  mocks.get.mockReset();
  mocks.decide.mockReset();
  mocks.list.mockResolvedValue({
    items: [],
    page: 1,
    page_size: 20,
    total: 0,
    has_next: false,
  });
  mocks.get.mockResolvedValue(detail);
});

afterEach(() => vi.restoreAllMocks());

describe('Approval beginner UX helpers', () => {
  it('documents review flow and status next actions without inventing Domain values', () => {
    expect(APPROVAL_REVIEW_FLOW_LABEL).toMatch(/요청 확인/);
    expect(APPROVAL_REVIEW_FLOW_LABEL).toMatch(/내용·위험 검토/);
    expect(APPROVAL_REVIEW_FLOW_LABEL).toMatch(/승인\/거절/);
    expect(approvalVsExecutionNote()).toMatch(/WAITING_APPROVAL/);
    expect(approvalVsExecutionNote()).toMatch(/즉시 RUNNING/);
    for (const status of [
      'PENDING',
      'APPROVED',
      'REJECTED',
      'EXPIRED',
      'CANCELLED',
    ] as const) {
      const guide = approvalStatusGuidance(status);
      expect(guide?.meaning).toBeTruthy();
      expect(guide?.nextAction).toBeTruthy();
    }
    expect(approvalStatusGuidance('APPROVED')?.nextAction).toMatch(/즉시 실행 완료|RUNNING/);
    expect(approvalsListEmptyGuidance('PENDING').title).toMatch(/대기 중/);
    expect(formatApprovalDecisionError({ status: 409, message: '' }).message).toMatch(
      /중복|충돌/,
    );
    expect(formatApprovalDecisionError({ status: 403 }).message).toMatch(/권한/);
  });
});

describe('Approvals beginner list banners', () => {
  it('shows Korean review flow and Approval≠Execution note', async () => {
    renderWithRouter(<Approvals />, { path: '/approvals', route: '/approvals' });
    expect(await screen.findByTestId('approval-setup-progress')).toBeInTheDocument();
    expect(screen.getByTestId('approval-setup-progress')).toHaveTextContent(/요청 확인/);
    expect(screen.getByTestId('approval-vs-execution-note')).toHaveTextContent(
      /Approval 상태/,
    );
    expect(screen.getByTestId('approval-vs-execution-note')).toHaveTextContent(
      /즉시 RUNNING/,
    );
    expect(await screen.findByText('대기 중인 승인 요청이 없습니다.')).toBeInTheDocument();
  });
});

describe('ApprovalDetail beginner status guidance', () => {
  it('shows PENDING guidance and keeps approve/reject available when can_decide', async () => {
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByTestId('approval-status-guidance')).toBeInTheDocument();
    expect(screen.getByTestId('approval-status-next')).toHaveTextContent(/검토/);
    expect(screen.getByTestId('approval-vs-execution-note')).toHaveTextContent(
      /PENDING을 WAITING_APPROVAL로 바꾸어 표시하지 않습니다/,
    );
    expect(screen.getAllByRole('button', { name: '승인' }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: '거절' }).length).toBeGreaterThan(0);
    expect(screen.queryByText('WAITING_APPROVAL')).not.toBeInTheDocument();
  });

  it('explains EXPIRED without offering decide controls', async () => {
    mocks.get.mockResolvedValue({
      ...detail,
      status: 'EXPIRED',
      can_decide: false,
    });
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });
    expect(await screen.findByTestId('approval-status-guidance')).toHaveTextContent(/만료/);
    expect(screen.queryByRole('button', { name: '승인' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '거절' })).not.toBeInTheDocument();
  });

  it('shows forbidden next-action guidance on 403', async () => {
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
    expect(await screen.findByTestId('approval-detail-forbidden-guidance')).toHaveTextContent(
      /approval\.decide/,
    );
  });
});
