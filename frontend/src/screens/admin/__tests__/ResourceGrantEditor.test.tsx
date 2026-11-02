import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ResourceGrantEditor from '../ResourceGrantEditor';
import { listMCPTools } from '../../../api/mcp';
import {
  createResourceGrant,
  deleteResourceGrant,
  listResourceGrants,
} from '../../../api/administration';

vi.mock('../../../api/mcp', () => ({ listMCPTools: vi.fn() }));
vi.mock('../../../api/administration', () => ({
  createResourceGrant: vi.fn(),
  deleteResourceGrant: vi.fn(),
  listResourceGrants: vi.fn(),
}));

const grant = {
  id: 'grant-1', user_id: 'user-1', role_id: null,
  resource_type: 'MCP_TOOL' as const, resource_id: 'tool-1', created_at: '',
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listResourceGrants).mockResolvedValue([]);
  vi.mocked(listMCPTools).mockResolvedValue({
    items: [
      { id: 'tool-1', remote_name: 'notion_list_pages', display_name: 'Notion 페이지 목록', status: 'ACTIVE' },
      { id: 'tool-2', remote_name: 'unpublished', display_name: '미공개 도구', status: 'DISCOVERED' },
    ],
    page: 1, page_size: 100, total: 2, has_next: false,
  } as Awaited<ReturnType<typeof listMCPTools>>);
  vi.mocked(createResourceGrant).mockResolvedValue(grant);
  vi.mocked(deleteResourceGrant).mockResolvedValue(undefined);
});

describe('ResourceGrantEditor', () => {
  it('offers only active MCP tools and creates an exact MCP_TOOL grant', async () => {
    vi.mocked(listResourceGrants).mockResolvedValueOnce([]).mockResolvedValueOnce([grant]);
    render(<ResourceGrantEditor owner="user" ownerId="user-1" />);
    const select = await screen.findByRole('combobox', { name: '권한을 부여할 MCP 도구' });
    expect(screen.getByRole('option', { name: /Notion 페이지 목록/ })).toBeTruthy();
    expect(screen.queryByRole('option', { name: /미공개 도구/ })).toBeNull();

    fireEvent.change(select, { target: { value: 'tool-1' } });
    fireEvent.click(screen.getByRole('button', { name: '접근 권한 부여' }));
    await waitFor(() => expect(createResourceGrant).toHaveBeenCalledWith('user', 'user-1', 'MCP_TOOL', 'tool-1'));
    expect(await screen.findByText('도구 접근 권한을 부여했습니다.')).toBeTruthy();
  });

  it('requires an explicit confirmation to revoke a grant', async () => {
    vi.mocked(listResourceGrants).mockResolvedValueOnce([grant]).mockResolvedValueOnce([]);
    render(<ResourceGrantEditor owner="user" ownerId="user-1" />);
    fireEvent.click(await screen.findByRole('button', { name: '해제' }));
    expect(deleteResourceGrant).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '해제 확인' }));
    await waitFor(() => expect(deleteResourceGrant).toHaveBeenCalledWith('user', 'user-1', 'grant-1'));
  });
});
