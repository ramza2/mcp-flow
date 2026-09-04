import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import StatusBadge from '@/components/ui/StatusBadge';

describe('StatusBadge presentation', () => {
  const cases: Array<{ status: string; label: string | RegExp }> = [
    { status: 'PENDING', label: 'PENDING' },
    { status: 'RUNNING', label: 'RUNNING' },
    { status: 'WAITING_INPUT', label: /Waiting Input/i },
    { status: 'WAITING_APPROVAL', label: /Waiting Approval/i },
    { status: 'SUCCEEDED', label: 'SUCCEEDED' },
    { status: 'PARTIALLY_SUCCEEDED', label: /Partial Success/i },
    { status: 'FAILED', label: 'FAILED' },
    { status: 'CANCELLED', label: 'CANCELLED' },
    { status: 'UNKNOWN_OUTCOME', label: /Unknown Outcome/i },
    { status: 'PUBLISHED', label: 'PUBLISHED' },
    { status: 'DEPRECATED', label: 'DEPRECATED' },
  ];

  for (const { status, label } of cases) {
    it(`renders visible text for ${status} without crashing`, () => {
      render(<StatusBadge status={status} />);
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  }
});
