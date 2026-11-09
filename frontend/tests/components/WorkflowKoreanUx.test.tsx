import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Workflows from '@/screens/build/Workflows';
import WorkflowDetail from '@/screens/build/WorkflowDetail';
import WorkflowDesigner from '@/screens/build/WorkflowDesigner';
import {
  labelWorkflowStatus,
  labelWorkflowVersionStatus,
  WORKFLOW_STATUSES,
  WORKFLOW_VERSION_STATUSES,
} from '@/domain';
import { renderWithRouter } from '../test-utils';

describe('Workflow Korean UX — presentation-only and mock safety', () => {
  it('translates canonical statuses without changing their values', () => {
    expect(WORKFLOW_STATUSES).toEqual(['DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED']);
    expect(WORKFLOW_VERSION_STATUSES).toEqual(['DRAFT', 'PUBLISHED', 'DEPRECATED']);
    expect(labelWorkflowStatus('ACTIVE')).toBe('활성');
    expect(labelWorkflowVersionStatus('PUBLISHED')).toBe('게시됨');
    expect(labelWorkflowStatus('SOMETHING_NEW')).toBe('SOMETHING_NEW');
  });

  it('labels mock workflows honestly and filters client-side by name/status', async () => {
    const user = userEvent.setup();
    renderWithRouter(<Workflows />, { path: '/workflows', route: '/workflows' });

    expect(screen.getByText('워크플로 관리')).toBeInTheDocument();
    expect(screen.getByText(/새로 만든 워크플로와 변경 사항은 서버에 저장되지 않으며/i)).toBeInTheDocument();
    expect(screen.getByText('Weekly Report Workflow')).toBeInTheDocument();
    expect(screen.getByText('Approval & Send Workflow')).toBeInTheDocument();

    await user.selectOptions(screen.getByRole('combobox', { name: '상태' }), 'DRAFT');
    expect(screen.getByText('Approval & Send Workflow')).toBeInTheDocument();
    expect(screen.queryByText('Weekly Report Workflow')).not.toBeInTheDocument();

    await user.type(screen.getByPlaceholderText('워크플로 이름 검색...'), 'absent');
    expect(screen.getByText('조건에 맞는 워크플로 예시가 없습니다.')).toBeInTheDocument();
  });

  it('does not present an operational Publish action on a demo detail', () => {
    renderWithRouter(<WorkflowDetail />, {
      path: '/workflows/:workflowId',
      route: '/workflows/wf-003',
    });

    expect(screen.getByText(/디자이너의 변경 사항은 저장되지 않고/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '게시 (미지원)' })).toBeDisabled();
    expect(screen.getByText('개요')).toBeInTheDocument();
  });

  it('makes unsaved designer behavior explicit and preserves JOIN option value', async () => {
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
