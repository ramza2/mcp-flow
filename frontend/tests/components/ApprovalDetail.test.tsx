import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ApprovalDetail from '@/screens/work/ApprovalDetail';
import { renderWithRouter } from '../test-utils';

describe('ApprovalDetail contract regression', () => {
  it('shows Approval Entity PENDING and never WAITING_APPROVAL as approval status', () => {
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: '/approvals/apr-001',
    });

    expect(screen.getByText('PENDING')).toBeInTheDocument();
    // WAITING_APPROVAL may appear in explanatory copy about Execution, but badge must be PENDING.
    const badges = screen.getAllByText('PENDING');
    expect(badges.length).toBeGreaterThan(0);
    expect(screen.queryByText('WAITING_APPROVAL')).not.toBeInTheDocument();
  });

  it('disables reject confirm when comment is required and empty, then shows REJECTED (not APPROVED)', async () => {
    const user = userEvent.setup();
    renderWithRouter(<ApprovalDetail />, {
      path: '/approvals/:approvalId',
      route: '/approvals/apr-001',
    });

    await user.click(screen.getAllByRole('button', { name: '거절' })[0]);
    expect(screen.getByText('승인 거절')).toBeInTheDocument();
    const confirm = screen.getByRole('button', { name: '거절 확정' });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByPlaceholderText(/거절 사유/i), '정책상 거절');
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    expect(await screen.findByText(/거절 완료/i)).toBeInTheDocument();
    expect(screen.getByText('REJECTED')).toBeInTheDocument();
    expect(screen.queryByText('APPROVED')).not.toBeInTheDocument();
    expect(screen.queryByText(/승인 완료/i)).not.toBeInTheDocument();
  });
});
