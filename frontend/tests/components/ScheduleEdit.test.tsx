import { describe, expect, it } from 'vitest';
import { screen, within } from '@testing-library/react';
import ScheduleEdit from '@/screens/work/ScheduleEdit';
import { renderWithRouter } from '../test-utils';

describe('ScheduleEdit Canonical options', () => {
  it('exposes Canonical target/overlap/misfire options and ACTIVE/PAUSED (not INACTIVE)', () => {
    renderWithRouter(<ScheduleEdit />, {
      path: '/schedules/new',
      route: '/schedules/new',
    });

    expect(screen.getByText('Agent Version')).toBeInTheDocument();
    expect(screen.getByText('Workflow Version')).toBeInTheDocument();

    const selects = screen.getAllByRole('combobox');
    const overlap = selects.find(el => within(el).queryByRole('option', { name: 'ALLOW' }));
    const misfire = selects.find(el => within(el).queryByRole('option', { name: 'CATCH_UP_LIMITED' }));
    expect(overlap).toBeTruthy();
    expect(misfire).toBeTruthy();

    for (const value of ['ALLOW', 'SKIP', 'QUEUE', 'REPLACE'] as const) {
      expect(within(overlap!).getByRole('option', { name: value })).toBeInTheDocument();
    }
    for (const value of ['SKIP', 'RUN_ONCE', 'CATCH_UP_LIMITED'] as const) {
      expect(within(misfire!).getByRole('option', { name: value })).toBeInTheDocument();
    }

    expect(screen.queryByRole('option', { name: 'CANCEL_RUNNING' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'RUN_ALL' })).not.toBeInTheDocument();

    expect(screen.getByText('ACTIVE')).toBeInTheDocument();
    expect(screen.queryByText('INACTIVE')).not.toBeInTheDocument();
  });
});
