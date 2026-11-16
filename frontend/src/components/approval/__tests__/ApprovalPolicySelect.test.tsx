import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ApprovalPolicySelect from '../ApprovalPolicySelect';

function ok(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const activePolicy = {
  id: '11111111-1111-4111-8111-111111111111',
  code: 'external-transfer',
  name: '외부 전송 승인',
  description: null,
  status: 'ACTIVE',
  decision_mode: 'ANY',
  required_approvals: 1,
  approver_scope: null,
  default_expiry_seconds: 3600,
  allow_self_approval: false,
  reject_comment_required: false,
  created_at: '2026-11-16T00:00:00Z',
  updated_at: '2026-11-16T00:00:00Z',
  lock_version: 1,
};

const inactivePolicy = {
  ...activePolicy,
  id: '22222222-2222-4222-8222-222222222222',
  code: 'legacy-approve',
  name: '레거시 승인',
  status: 'INACTIVE',
  lock_version: 3,
};

describe('ApprovalPolicySelect', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('lists ACTIVE policies and reports selection', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const fetchMock = vi.fn().mockResolvedValue(
      ok({ items: [activePolicy], page: 1, page_size: 100, total: 1, has_next: false }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<ApprovalPolicySelect value={null} onChange={onChange} />);

    const select = await screen.findByRole('combobox', { name: '승인 정책' });
    await waitFor(() => {
      expect(select).not.toBeDisabled();
    });
    expect(screen.getByRole('option', { name: /외부 전송 승인/ })).toBeInTheDocument();

    await user.selectOptions(select, activePolicy.id);
    expect(onChange).toHaveBeenCalledWith(activePolicy.id);

    const listUrl = String(fetchMock.mock.calls[0][0]);
    expect(listUrl).toContain('/api/v1/approval-policies?');
    expect(listUrl).toContain('status=ACTIVE');
  });

  it('restores inactive current selection via detail GET', async () => {
    const fetchMock = vi.fn((input: RequestInfo) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/approval-policies?')) {
        return Promise.resolve(
          ok({ items: [activePolicy], page: 1, page_size: 100, total: 1, has_next: false }),
        );
      }
      if (url.endsWith(`/approval-policies/${inactivePolicy.id}`)) {
        return Promise.resolve(ok(inactivePolicy));
      }
      return Promise.resolve(new Response('missing', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <ApprovalPolicySelect value={inactivePolicy.id} onChange={vi.fn()} />,
    );

    expect(
      await screen.findByRole('option', { name: /레거시 승인.*비활성.*현재 연결/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/현재 연결된 승인 정책이 비활성 상태입니다/),
    ).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toHaveValue(inactivePolicy.id);
  });

  it('shows 403 guidance, disables select, and keeps orphan current value', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ok(
        {
          error: {
            code: 'AUTH_FORBIDDEN',
            message: 'Missing approval.policy.manage permission.',
          },
        },
        403,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(
      <ApprovalPolicySelect value={inactivePolicy.id} onChange={vi.fn()} />,
    );

    expect(
      await screen.findByText(/승인 정책 목록을 볼 권한이 없습니다/),
    ).toBeInTheDocument();
    const select = screen.getByRole('combobox', { name: '승인 정책' });
    expect(select).toBeDisabled();
    expect(select).toHaveValue(inactivePolicy.id);
    expect(
      screen.getByRole('option', { name: /삭제되었거나 조회할 수 없음/ }),
    ).toBeInTheDocument();
  });
});
