import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AgentRun from '@/screens/work/AgentRun';
import { renderWithRouter } from '../test-utils';

describe('AgentRun — AgentRequest vs Execution separation', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders separate Agent Request Status and Execution Status panels', () => {
    renderWithRouter(<AgentRun />, { path: '/run', route: '/run' });
    expect(screen.getByText('Agent Request Status')).toBeInTheDocument();
    expect(screen.getByText('Execution Status')).toBeInTheDocument();
  });

  it('shows Planning WAITING_INPUT as Agent needs information (not MCP Tool)', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTimeAsync });
    renderWithRouter(<AgentRun />, { path: '/run', route: '/run' });

    const input = screen.getAllByPlaceholderText(/업무를 요청/i)[0];
    await user.type(input, '주간 보고서 생성');
    await user.click(screen.getAllByRole('button', { name: /전송/i })[0]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    await waitFor(() => {
      expect(screen.getAllByText('Agent needs information').length).toBeGreaterThan(0);
    });

    expect(screen.queryByText('MCP Tool requests information')).not.toBeInTheDocument();
    expect(screen.getByText(/AgentRequest Status: WAITING_INPUT \(Planning\)/i)).toBeInTheDocument();
  });
});
