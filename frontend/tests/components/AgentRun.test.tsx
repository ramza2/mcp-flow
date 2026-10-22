import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AgentRun from '@/screens/work/AgentRun';
import { DEMO_REQUEST } from '@/screens/work/agentRunDemo';
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
    expect(screen.getByText('Agent Request (시뮬레이션)')).toBeInTheDocument();
    expect(screen.getByText('Execution (시뮬레이션)')).toBeInTheDocument();
    expect(screen.getByText(/데모 화면 — 실제 실행 아님/)).toBeInTheDocument();
  });

  it('shows Planning WAITING_INPUT as Agent needs information (not MCP Tool)', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTimeAsync });
    renderWithRouter(<AgentRun />, { path: '/run', route: '/run' });

    const input = screen.getByPlaceholderText(/고정 예시 문장만/);
    await user.type(input, DEMO_REQUEST);
    await user.click(screen.getByRole('button', { name: '데모 실행' }));

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
