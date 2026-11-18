/**
 * Frontend PermissionGate is UX only.
 * Backend authorization remains authoritative.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import PermissionGate from '@/components/PermissionGate';
import { buildAuthContext } from '../fixtures/auth-context';

const useAuthMock = vi.fn();

vi.mock('@/auth/useAuth', () => ({
  useAuth: () => useAuthMock(),
}));

describe('PermissionGate (UX helper only — Backend auth remains authoritative)', () => {
  beforeEach(() => {
    useAuthMock.mockReset();
  });

  it('shows children when session permission is allowed', () => {
    useAuthMock.mockReturnValue(buildAuthContext({ permissions: ['approval.decide'] }));
    render(
      <PermissionGate permission="approval.decide">
        <button type="button">Approve</button>
      </PermissionGate>,
    );
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('hides children by default when permission is denied (mode=hide)', () => {
    useAuthMock.mockReturnValue(buildAuthContext({ permissions: [] }));
    render(
      <PermissionGate permission="audit.read">
        <button type="button">Secret Admin</button>
      </PermissionGate>,
    );
    expect(screen.queryByRole('button', { name: 'Secret Admin' })).not.toBeInTheDocument();
  });

  it('keeps children visible but aria-disabled when mode=disable', () => {
    useAuthMock.mockReturnValue(buildAuthContext({ permissions: [] }));
    render(
      <PermissionGate permission="execution.cancel" mode="disable">
        <button type="button">Cancel</button>
      </PermissionGate>,
    );
    const wrapper = screen.getByTitle(/권한이 없습니다 \(UX only\)/i);
    expect(wrapper).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });
});
