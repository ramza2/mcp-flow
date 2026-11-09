import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import Agents, { NEW_AGENT_PATH } from '@/screens/build/Agents';
import { renderWithRouter } from '../test-utils';

vi.mock('@/api/agents', () => ({
  listAgents: vi.fn(),
  createAgent: vi.fn(),
}));

import { createAgent, listAgents } from '@/api/agents';

describe('Agents New Agent local draft', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAgents).mockResolvedValue({
      items: [],
      page: 1,
      page_size: 20,
      total: 0,
      has_next: false,
    });
  });

  it('새 에이전트 navigates to local form without POST /agents', async () => {
    const { router } = renderWithRouter(<Agents />, {
      path: '/agents',
      route: '/agents',
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '새 에이전트' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '새 에이전트' }));

    expect(createAgent).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe(NEW_AGENT_PATH);
  });

  it('renders friendly agent status and visibility without changing filters', async () => {
    vi.mocked(listAgents).mockResolvedValue({
      items: [{
        id: 'agent-1', code: 'test', name: '연구 에이전트',
        description: null, status: 'ACTIVE', visibility: 'PRIVATE',
        current_version_id: null, owner_id: 'user-1',
        created_at: '2026-11-09T00:00:00Z', created_by: 'user-1',
        updated_at: '2026-11-09T00:00:00Z', updated_by: 'user-1',
        lock_version: 1,
      }],
      page: 1, page_size: 20, total: 1, has_next: false,
    });

    renderWithRouter(<Agents />, { path: '/agents', route: '/agents' });

    expect(await screen.findByText('연구 에이전트')).toBeInTheDocument();
    expect(screen.getByText('비공개 (나만 보기)')).toHaveAttribute('title', 'PRIVATE');
    expect(screen.getAllByText('활성')).toHaveLength(2);
    expect(screen.getByText(/1페이지/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '이전' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled();
  });
});
