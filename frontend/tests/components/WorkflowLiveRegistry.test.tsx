import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Workflows from '@/screens/build/Workflows';
import WorkflowNew from '@/screens/build/WorkflowNew';
import WorkflowDetail, { initialWorkflowPlan } from '@/screens/build/WorkflowDetail';
import WorkflowPlanEditor from '@/screens/build/WorkflowPlanEditor';
import { renderWithRouter } from '../test-utils';

vi.mock('@/api/workflows', () => ({
  listWorkflows: vi.fn(),
  createWorkflow: vi.fn(),
  getWorkflow: vi.fn(),
  listWorkflowVersions: vi.fn(),
  createWorkflowVersion: vi.fn(),
  getWorkflowVersion: vi.fn(),
  saveWorkflowPlan: vi.fn(),
  validateWorkflowVersion: vi.fn(),
}));
vi.mock('@/api/mcp', () => ({ listMCPTools: vi.fn() }));

import {
  listWorkflows, createWorkflow, getWorkflow, listWorkflowVersions,
  createWorkflowVersion, getWorkflowVersion, saveWorkflowPlan, validateWorkflowVersion,
  type WorkflowDto, type WorkflowVersionDto,
} from '@/api/workflows';
import { listMCPTools } from '@/api/mcp';

const wfId = '11111111-1111-4111-8111-111111111111';
const vId = '22222222-2222-4222-8222-222222222222';

const workflow: WorkflowDto = {
  id: wfId, code: 'wf-test', name: '업무 자동화', description: '매일 검토', status: 'DRAFT',
  current_version_id: null, visibility: 'PRIVATE', owner_id: null,
  created_at: '2026-11-09T00:00:00Z', created_by: null,
  updated_at: '2026-11-09T00:00:00Z', updated_by: null, lock_version: 1,
};

const version: WorkflowVersionDto = {
  id: vId, workflow_id: wfId, version_no: 1, status: 'DRAFT',
  plan_schema_version: '1.0', plan_definition: initialWorkflowPlan(workflow),
  input_schema: {}, output_schema: {}, policy_defaults: {},
  validation_status: 'INVALID', validation_report: null, content_hash: 'hash',
  change_summary: '초기 초안', published_at: null, published_by: null,
  deprecated_at: null, deprecated_by: null,
  created_at: '2026-11-09T00:00:00Z', created_by: null,
};

describe('Workflow real registry — no mock IDs or pretend save', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listMCPTools).mockResolvedValue({ items: [], page: 1, page_size: 100, total: 0, has_next: false });
    vi.mocked(listWorkflows).mockResolvedValue({ items: [workflow], page: 1, page_size: 20, total: 1, has_next: false });
    vi.mocked(getWorkflow).mockResolvedValue(workflow);
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [version], page: 1, page_size: 100, total: 1, has_next: false });
    vi.mocked(getWorkflowVersion).mockResolvedValue(version);
    vi.mocked(saveWorkflowPlan).mockResolvedValue(version);
    vi.mocked(validateWorkflowVersion).mockResolvedValue({
      id: vId, workflow_id: wfId, version_no: 1, status: 'DRAFT',
      validation_status: 'VALID', validation_report: null, content_hash: 'hash',
    });
    vi.mocked(createWorkflowVersion).mockResolvedValue(version);
    vi.mocked(createWorkflow).mockResolvedValue(workflow);
  });

  it('loads live data and maps filter to API canonical values', async () => {
    const user = userEvent.setup();
    renderWithRouter(<Workflows />, { path: '/workflows', route: '/workflows' });
    expect(await screen.findByText('업무 자동화')).toBeInTheDocument();
    expect(screen.queryByText('Weekly Report Workflow')).not.toBeInTheDocument();
    expect(screen.getByText('비공개 (나만 보기)')).toHaveAttribute('title', 'PRIVATE');
    await user.selectOptions(screen.getByRole('combobox', { name: '상태' }), 'ACTIVE');
    await waitFor(() => expect(listWorkflows).toHaveBeenCalledWith(expect.objectContaining({ status: 'ACTIVE' })));
    await user.type(screen.getByPlaceholderText('워크플로 이름 검색...'), '보고서');
    await waitFor(() => expect(listWorkflows).toHaveBeenCalledWith(expect.objectContaining({ q: '보고서' })));
  });

  it('does not POST before user registers the workflow', async () => {
    const user = userEvent.setup();
    const { router } = renderWithRouter(<WorkflowNew />, {
      routes: [
        { path: '/workflows/new', element: <WorkflowNew /> },
        { path: `/workflows/${wfId}`, element: <div>등록 완료</div> },
      ],
      route: '/workflows/new',
    });
    expect(createWorkflow).not.toHaveBeenCalled();
    await user.type(screen.getByRole('textbox', { name: '이름 (필수)' }), '업무 자동화');
    await user.click(screen.getByRole('button', { name: '워크플로 등록' }));
    await waitFor(() => expect(createWorkflow).toHaveBeenCalledWith({
      name: '업무 자동화', description: null, visibility: 'PRIVATE',
    }));
    expect(router.state.location.pathname).toBe(`/workflows/${wfId}`);
  });

  it('shows actual versions and edits DRAFT by exact UUID, without automatic writes', async () => {
    const user = userEvent.setup();
    const { router } = renderWithRouter(<WorkflowDetail />, {
      route: `/workflows/${wfId}`,
      routes: [
        { path: '/workflows/:workflowId', element: <WorkflowDetail /> },
        { path: '/workflows/:workflowId/versions/:versionId/edit', element: <div>Plan Editor</div> },
      ],
    });
    expect(await screen.findByText('업무 자동화')).toBeInTheDocument();
    expect(screen.getByText(/단계 흐름의 시각 편집과 고급 JSON 편집을 지원/)).toBeInTheDocument();
    expect(createWorkflowVersion).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '초안 편집' }));
    expect(router.state.location.pathname).toBe(`/workflows/${wfId}/versions/${vId}/edit`);
    expect(createWorkflowVersion).not.toHaveBeenCalled();
  });

  it('creates a server DRAFT only when the user clicks, with owned initial Plan', async () => {
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [], page: 1, page_size: 100, total: 0, has_next: false });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '새 초안 버전 생성' }));
    await waitFor(() => expect(createWorkflowVersion).toHaveBeenCalledWith(wfId, {
      plan_definition: expect.objectContaining({ source: { type: 'WORKFLOW', workflow_id: wfId }, steps: [] }),
      change_summary: '초기 초안',
    }));
  });

  it('saves JSON to exact DRAFT version, blocks validation while dirty', async () => {
    const user = userEvent.setup();
    renderWithRouter(<WorkflowPlanEditor />, {
      path: '/workflows/:workflowId/versions/:versionId/edit',
      route: `/workflows/${wfId}/versions/${vId}/edit`,
    });
    await user.click(await screen.findByRole('button', { name: '고급 JSON' }));
    const input = screen.getByRole('textbox', { name: '실행 계획 JSON' });
    fireEvent.change(input, { target: { value: JSON.stringify({ ...version.plan_definition, goal: '새 목표' }) } });
    expect(screen.getByRole('button', { name: '서버 검증' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '초안 저장' }));
    await waitFor(() => expect(saveWorkflowPlan).toHaveBeenCalledWith(wfId, vId, expect.objectContaining({ goal: '새 목표' })));
    expect(validateWorkflowVersion).not.toHaveBeenCalled();
  });

  it('rejects malformed JSON and refuses stale-tab overwrite', async () => {
    vi.mocked(getWorkflowVersion)
      .mockResolvedValueOnce(version)
      .mockResolvedValueOnce({ ...version, content_hash: 'changed-on-server' });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowPlanEditor />, {
      path: '/workflows/:workflowId/versions/:versionId/edit',
      route: `/workflows/${wfId}/versions/${vId}/edit`,
    });
    await user.click(await screen.findByRole('button', { name: '고급 JSON' }));
    const input = screen.getByRole('textbox', { name: '실행 계획 JSON' });
    fireEvent.change(input, { target: { value: '{not-json' } });
    await user.click(screen.getByRole('button', { name: '초안 저장' }));
    expect(screen.getByText(/JSON 문법이 잘못되었습니다/)).toBeInTheDocument();
    expect(saveWorkflowPlan).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: JSON.stringify({ ...version.plan_definition, goal: 'modified' }) } });
    await user.click(screen.getByRole('button', { name: '초안 저장' }));
    expect(await screen.findByText(/다른 곳에서 버전이 변경되었거나 게시되었습니다/)).toBeInTheDocument();
    expect(saveWorkflowPlan).not.toHaveBeenCalled();
  });

  it('uses the server validator only for unchanged DRAFT, without publishing', async () => {
    const user = userEvent.setup();
    renderWithRouter(<WorkflowPlanEditor />, {
      path: '/workflows/:workflowId/versions/:versionId/edit',
      route: `/workflows/${wfId}/versions/${vId}/edit`,
    });
    await screen.findByRole('button', { name: '시각 편집' });
    await user.click(screen.getByRole('button', { name: '서버 검증' }));
    await waitFor(() => expect(validateWorkflowVersion).toHaveBeenCalledWith(wfId, vId));
    expect(await screen.findByText(/서버 검증을 통과했습니다/)).toBeInTheDocument();
  });

  it('protects PUBLISHED version from PUT /plan and validation writes', async () => {
    vi.mocked(getWorkflowVersion).mockResolvedValue({ ...version, status: 'PUBLISHED' });
    renderWithRouter(<WorkflowPlanEditor />, {
      path: '/workflows/:workflowId/versions/:versionId/edit',
      route: `/workflows/${wfId}/versions/${vId}/edit`,
    });
    await screen.findByRole('button', { name: '고급 JSON' });
    fireEvent.click(screen.getByRole('button', { name: '고급 JSON' }));
    expect(screen.getByRole('textbox', { name: '실행 계획 JSON' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '초안 저장' })).not.toBeInTheDocument();
    expect(saveWorkflowPlan).not.toHaveBeenCalled();
    expect(validateWorkflowVersion).not.toHaveBeenCalled();
  });
});
