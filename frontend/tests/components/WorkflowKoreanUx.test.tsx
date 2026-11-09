import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WorkflowDesigner from '@/screens/build/WorkflowDesigner';
import {
  labelWorkflowStatus,
  labelWorkflowVersionStatus,
  WORKFLOW_STATUSES,
  WORKFLOW_VERSION_STATUSES,
} from '@/domain';
import { renderWithRouter } from '../test-utils';

describe('Workflow Korean UX — canonical labels and legacy demo isolation', () => {
  it('translates canonical statuses without changing their values', () => {
    expect(WORKFLOW_STATUSES).toEqual(['DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED']);
    expect(WORKFLOW_VERSION_STATUSES).toEqual(['DRAFT', 'PUBLISHED', 'DEPRECATED']);
    expect(labelWorkflowStatus('ACTIVE')).toBe('활성');
    expect(labelWorkflowVersionStatus('PUBLISHED')).toBe('게시됨');
    expect(labelWorkflowStatus('SOMETHING_NEW')).toBe('SOMETHING_NEW');
  });

  it('keeps legacy demo isolated from operational routes and preserves JOIN values', async () => {
    const user = userEvent.setup();
    const { router } = renderWithRouter(<WorkflowDesigner />, {
      path: '/workflows/:workflowId/versions/:versionId/edit',
      route: '/workflows/wf-003/versions/v1/edit',
    });

    expect(screen.getByText('워크플로 디자이너')).toBeInTheDocument();
    expect(screen.getByText(/서버 저장·게시·실제 실행은 연결되지 않았습니다/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Draft 저장' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /흐름 합치기/ }));
    const policy = screen.getByText('흐름 합치기 정책').parentElement?.querySelector('select') as HTMLSelectElement;
    expect(policy.value).toBe('ALL_SUCCESS');
    expect(screen.getByRole('option', { name: '모두 완료되면 합치기' })).toHaveValue('ALL_COMPLETE');

    await user.click(screen.getByRole('button', { name: '시연 종료 (저장 안 됨)' }));
    expect(router.state.location.pathname).toBe('/workflows/wf-003');
  });
});
