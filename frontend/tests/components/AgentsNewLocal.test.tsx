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

  it('New Agent navigates to local form without POST /agents', async () => {
    const { router } = renderWithRouter(<Agents />, {
      path: '/agents',
      route: '/agents',
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /New Agent/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /New Agent/i }));

    expect(createAgent).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe(NEW_AGENT_PATH);
  });
});
