import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ExecutionDetail from '@/screens/work/ExecutionDetail';
import { renderWithRouter } from '../test-utils';

describe('ExecutionDetail MRTR / UNKNOWN_OUTCOME contracts', () => {
  it('MRTR: WAITING_INPUT → respond → RUNNING without exposing opaque requestState payload', async () => {
    const user = userEvent.setup();
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: '/executions/EXE-20260902-00126',
    });

    expect(screen.getByText('Waiting Input')).toBeInTheDocument();
    expect(screen.getByText(/MCP Tool requests information \(Runtime WAITING_INPUT\)/i)).toBeInTheDocument();
    expect(screen.getByText(/requestState is not user-visible/i)).toBeInTheDocument();
    // Opaque payload must not appear as editable/user-facing data.
    expect(screen.queryByDisplayValue(/requestState/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /requestState/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /응답 후 Resume/i }));

    expect(await screen.findByText('RUNNING')).toBeInTheDocument();
    expect(screen.getByText(/응답 제출됨 — Execution RUNNING으로 재개/i)).toBeInTheDocument();
  });

  it('UNKNOWN_OUTCOME: shows ops guidance and hides automatic Retry CTA', () => {
    renderWithRouter(<ExecutionDetail />, {
      path: '/executions/:executionId',
      route: '/executions/EXE-20260901-00119',
    });

    expect(screen.getByText(/UNKNOWN_OUTCOME/i)).toBeInTheDocument();
    expect(screen.getByText(/자동 Retry CTA는 제공하지 않습니다/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /New Execution \(Retry\)/i })).not.toBeInTheDocument();
  });
});
