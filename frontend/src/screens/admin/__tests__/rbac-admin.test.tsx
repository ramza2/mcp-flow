import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import Roles from '../Roles';
import UserDetail from '../UserDetail';
import {
  getRole, getUser, listPermissions, listRolePermissions, listRoles,
  listUserRoles, replaceRolePermissions, replaceUserRoles,
} from '../../../api/administration';

vi.mock('../../../api/administration', () => ({
  getRole: vi.fn(),
  getUser: vi.fn(),
  listPermissions: vi.fn(),
  listRolePermissions: vi.fn(),
  listRoles: vi.fn(),
  listUserRoles: vi.fn(),
  replaceRolePermissions: vi.fn(),
  replaceUserRoles: vi.fn(),
}));
vi.mock('../ResourceGrantEditor', () => ({
  default: ({ owner, ownerId }: { owner: string; ownerId: string }) =>
    <div data-testid="grant-editor">{owner}:{ownerId}</div>,
}));

const role = {
  id: 'role-1', code: 'OPERATOR', name: '운영자', description: null,
  lock_version: 4, created_at: '', updated_at: '',
};
const permission = {
  id: 'perm-execute', code: 'mcp.tool.execute', name: 'MCP 도구 실행',
  description: null, created_at: '',
};
const user = {
  id: 'user-1', username: 'operator', display_name: '테스트 운영자',
  email: 'operator@example.com', status: 'ACTIVE' as const,
  last_login_at: null, created_at: '', updated_at: '', lock_version: 8,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listRoles).mockResolvedValue([role]);
  vi.mocked(listPermissions).mockResolvedValue([permission]);
  vi.mocked(listRolePermissions).mockResolvedValue([]);
  vi.mocked(getRole).mockResolvedValue(role);
  vi.mocked(getUser).mockResolvedValue(user);
  vi.mocked(listUserRoles).mockResolvedValue([]);
  vi.mocked(replaceRolePermissions).mockResolvedValue([permission]);
  vi.mocked(replaceUserRoles).mockResolvedValue([role]);
});

describe('Admin RBAC screens', () => {
  it('loads actual permission catalog and saves an exact selection with lock_version', async () => {
    render(<MemoryRouter><Roles /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('checkbox', { name: /mcp.tool.execute/ }));
    fireEvent.click(screen.getByRole('button', { name: '저장' }));
    await waitFor(() => expect(replaceRolePermissions).toHaveBeenCalledWith('role-1', ['perm-execute'], 4));
    fireEvent.click(screen.getByRole('button', { name: 'MCP 도구 접근 권한' }));
    expect(screen.getByTestId('grant-editor').textContent).toContain('role:role-1');
  });

  it('uses the actual user record and role assignment API, not mock users', async () => {
    render(
      <MemoryRouter initialEntries={['/admin/users/user-1']}>
        <Routes><Route path="/admin/users/:userId" element={<UserDetail />} /></Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText('operator@example.com')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '역할 관리' }));
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '역할 저장' }));
    await waitFor(() => expect(replaceUserRoles).toHaveBeenCalledWith('user-1', ['role-1'], 8));
    fireEvent.click(screen.getByRole('button', { name: 'MCP 도구 접근 권한' }));
    expect(screen.getByTestId('grant-editor').textContent).toContain('user:user-1');
  });
});
