// @vitest-environment jsdom
/**
 * Contract smoke for Approval Entity status presentation.
 * Full live API wiring coverage lives in ApprovalsLive.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import ApprovalDetail from '@/screens/work/ApprovalDetail';
import { renderWithRouter } from '../test-utils';

const mocks = vi.hoisted(() => ({ get: vi.fn(), decide: vi.fn() }));

vi.mock('@/api/approvals', () => ({
  getApproval: mocks.get,
  listApprovals: vi.fn(),
  createApprovalDecision: mocks.decide,
}));

const approvalId = '11111111-1111-4111-8111-111111111111';

beforeEach(() => {
  mocks.get.mockReset();
  mocks.decide.mockReset();
  mocks.get.mockResolvedValue({
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
    safe_context: {},
    decisions: [],
  });
});

afterEach(() => vi.restoreAllMocks());

describe('ApprovalDetail contract regression', () => {
  it('shows Approval Entity PENDING and never WAITING_APPROVAL as approval status', async () => {
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: `/approvals/${approvalId}`,
    });

    expect(await screen.findByText('현재 계정으로 결정 가능')).toBeInTheDocument();
    const pendingBadges = screen.getAllByText('대기');
    expect(pendingBadges.length).toBeGreaterThan(0);
    expect(pendingBadges.every((el) => el.getAttribute('title') === 'PENDING')).toBe(true);
    expect(screen.queryByText('WAITING_APPROVAL')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '승인' }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: '거절' }).length).toBeGreaterThan(0);
  });
});
