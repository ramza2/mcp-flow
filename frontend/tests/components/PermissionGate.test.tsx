/**
 * Frontend PermissionGate is UX only.
 * Backend authorization remains authoritative.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import PermissionGate from '@/components/PermissionGate';
import { hasPermission } from '@/domain';

vi.mock('@/domain', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/domain')>();
  return {
    ...actual,
    hasPermission: vi.fn(),
  };
});

const mockedHasPermission = vi.mocked(hasPermission);

describe('PermissionGate (UX helper only — Backend auth remains authoritative)', () => {
  beforeEach(() => {
    mockedHasPermission.mockReset();
  });

  it('shows children when permission is allowed', () => {
    mockedHasPermission.mockReturnValue(true);
    render(
      <PermissionGate permission="approval.approve">
        <button type="button">Approve</button>
      </PermissionGate>,
    );
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('hides children by default when permission is denied (mode=hide)', () => {
    mockedHasPermission.mockReturnValue(false);
    render(
      <PermissionGate permission="admin.manage">
        <button type="button">Secret Admin</button>
      </PermissionGate>,
    );
    expect(screen.queryByRole('button', { name: 'Secret Admin' })).not.toBeInTheDocument();
  });

  it('keeps children visible but aria-disabled when mode=disable', () => {
    mockedHasPermission.mockReturnValue(false);
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
