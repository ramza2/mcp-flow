import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import SafeReadOnlyGrantPicker from '../SafeReadOnlyGrantPicker';
import { getMCPToolPolicy } from '../../../api/mcp';
import { createResourceGrant } from '../../../api/administration';
import type { MCPToolDto, MCPToolPolicyDto } from '../../../api/types';

vi.mock('../../../api/mcp', () => ({ getMCPToolPolicy: vi.fn() }));
vi.mock('../../../api/administration', () => ({ createResourceGrant: vi.fn() }));

const safePolicy = {
  risk_class: 'READ_ONLY',
  allow_auto_select: true,
  requires_confirmation: false,
  requires_approval: false,
} as MCPToolPolicyDto;

function tool(id: string, status: MCPToolDto['status'] = 'ACTIVE'): MCPToolDto {
  return {
    id,
    remote_name: id,
    display_name: '도구 ' + id,
    status,
    current_version_id: 'version-' + id,
  } as MCPToolDto;
}

const onChanged = vi.fn(async () => {});
const setBusy = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getMCPToolPolicy).mockResolvedValue(safePolicy);
  vi.mocked(createResourceGrant).mockResolvedValue({} as Awaited<ReturnType<typeof createResourceGrant>>);
});

describe('SafeReadOnlyGrantPicker', () => {
  it('recommends only tools with an explicitly safe, readable policy', async () => {
    vi.mocked(getMCPToolPolicy).mockImplementation(async id => {
      if (id === 'read') return safePolicy;
      if (id === 'write') return { ...safePolicy, risk_class: 'IDEMPOTENT_WRITE' };
      if (id === 'confirmation') return { ...safePolicy, requires_confirmation: true };
      if (id === 'manual') return { ...safePolicy, allow_auto_select: false };
      throw new Error('Policy missing');
    });
    render(
      <SafeReadOnlyGrantPicker owner="role" ownerId="role-1"
        available={[tool('read'), tool('write'), tool('confirmation'), tool('manual'), tool('missing'), tool('inactive', 'INACTIVE')].filter(item => item.status === 'ACTIVE')}
        busy={false} setBusy={setBusy} onChanged={onChanged} />,
    );
    fireEvent.click(screen.getByRole('button', { name: '안전 조회 도구 찾기' }));
    expect(await screen.findByText(/안전 조회 후보 1개/)).toBeTruthy();
    expect(screen.getByText('도구 read')).toBeTruthy();
    expect(screen.queryByText('도구 write')).toBeNull();
    expect(screen.queryByText('도구 confirmation')).toBeNull();
    expect(screen.queryByText('도구 manual')).toBeNull();
    expect(screen.queryByText('도구 missing')).toBeNull();
    expect(createResourceGrant).not.toHaveBeenCalled();
  });

  it('does not create a role grant until selection and explicit confirmation', async () => {
    render(
      <SafeReadOnlyGrantPicker owner="role" ownerId="role-1"
        available={[tool('read')]} busy={false} setBusy={setBusy} onChanged={onChanged} />,
    );
    fireEvent.click(screen.getByRole('button', { name: '안전 조회 도구 찾기' }));
    await screen.findByText(/안전 조회 후보 1개/);
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '선택한 1개 접근 권한 부여' }));
    expect(createResourceGrant).not.toHaveBeenCalled();
    expect(screen.getByText(/이 역할을 가진 모든 사용자에게/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '부여 확인' }));
    await waitFor(() =>
      expect(createResourceGrant).toHaveBeenCalledWith('role', 'role-1', 'MCP_TOOL', 'read'));
    expect(onChanged).toHaveBeenCalledOnce();
    expect(setBusy).toHaveBeenCalledWith(true);
    expect(setBusy).toHaveBeenCalledWith(false);
  });

  it('rechecks policy and blocks a grant when it becomes unsafe', async () => {
    vi.mocked(getMCPToolPolicy)
      .mockResolvedValueOnce(safePolicy)
      .mockResolvedValueOnce({ ...safePolicy, requires_approval: true });
    render(
      <SafeReadOnlyGrantPicker owner="user" ownerId="user-1"
        available={[tool('read')]} busy={false} setBusy={setBusy} onChanged={onChanged} />,
    );
    fireEvent.click(screen.getByRole('button', { name: '안전 조회 도구 찾기' }));
    await screen.findByText(/안전 조회 후보 1개/);
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '선택한 1개 접근 권한 부여' }));
    fireEvent.click(screen.getByRole('button', { name: '부여 확인' }));
    await waitFor(() => expect(screen.getByText(/1개는 실패하거나 정책이 변경되어 제외/)).toBeTruthy());
    expect(createResourceGrant).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalledOnce();
  });
});
