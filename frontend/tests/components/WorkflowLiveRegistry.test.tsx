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
  publishWorkflowVersion: vi.fn(),
  changeWorkflowStatus: vi.fn(),
  createWorkflowExecution: vi.fn(),
}));
vi.mock('@/api/mcp', () => ({ listMCPTools: vi.fn() }));

import {
  listWorkflows, createWorkflow, getWorkflow, listWorkflowVersions,
  createWorkflowVersion, getWorkflowVersion, saveWorkflowPlan, validateWorkflowVersion, publishWorkflowVersion,
  changeWorkflowStatus, createWorkflowExecution,
  type WorkflowDto, type WorkflowVersionDto,
} from '@/api/workflows';
import { listMCPTools } from '@/api/mcp';

const wfId = '11111111-1111-4111-8111-111111111111';
const vId = '22222222-2222-4222-8222-222222222222';
const execId = '44444444-4444-4444-8444-444444444444';

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

const publishedVersion: WorkflowVersionDto = { ...version, status: 'PUBLISHED', validation_status: 'VALID' };

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
    vi.mocked(changeWorkflowStatus).mockResolvedValue({ ...workflow, status: 'ACTIVE', lock_version: 2 });
    vi.mocked(createWorkflowExecution).mockResolvedValue({
      id: execId, status: 'QUEUED', source_type: 'WORKFLOW_VERSION', trigger_type: 'MANUAL',
      workflow_version_id: vId, plan_hash: 'hash', requested_at: '2026-11-10T00:00:00Z', step_count: 1,
    });
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
    expect(screen.getByText(/초안은 저장 후 서버 검증을 통과해야 게시할 수 있습니다/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '게시' })).toBeDisabled();
    expect(publishWorkflowVersion).not.toHaveBeenCalled();
    expect(createWorkflowVersion).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '초안 편집' }));
    expect(router.state.location.pathname).toBe(`/workflows/${wfId}/versions/${vId}/edit`);
    expect(createWorkflowVersion).not.toHaveBeenCalled();
  });

  it('requires explicit confirmation and publishes only a matching server-validated DRAFT', async () => {
    const validDraft: WorkflowVersionDto = {
      ...version,
      validation_status: 'VALID',
      validation_report: { valid: true, content_hash: version.content_hash },
    };
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [validDraft], page: 1, page_size: 100, total: 1, has_next: false });
    vi.mocked(getWorkflowVersion).mockResolvedValue(validDraft);
    vi.mocked(publishWorkflowVersion).mockResolvedValue({ ...validDraft, status: 'PUBLISHED' });

    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '게시' }));
    expect(screen.getByText(/게시만으로 활성화되거나 실행되지는 않습니다/)).toBeInTheDocument();
    expect(publishWorkflowVersion).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '취소' }));
    expect(publishWorkflowVersion).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '게시' }));
    await user.click(screen.getByRole('button', { name: '게시 확정' }));
    await waitFor(() => expect(getWorkflowVersion).toHaveBeenCalledWith(wfId, vId));
    await waitFor(() => expect(publishWorkflowVersion).toHaveBeenCalledTimes(1));
    expect(publishWorkflowVersion).toHaveBeenCalledWith(wfId, vId);
    expect(await screen.findByRole('status')).toHaveTextContent('v1 게시가 완료되었습니다');
  });

  it('refuses publish when the persisted version changed since the screen loaded', async () => {
    const validDraft: WorkflowVersionDto = {
      ...version,
      validation_status: 'VALID',
      validation_report: { valid: true, content_hash: version.content_hash },
    };
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [validDraft], page: 1, page_size: 100, total: 1, has_next: false });
    vi.mocked(getWorkflowVersion).mockResolvedValue({ ...validDraft, content_hash: 'changed' });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '게시' }));
    await user.click(screen.getByRole('button', { name: '게시 확정' }));
    expect(await screen.findByText(/게시 상태 또는 검증 결과가 변경되었습니다/)).toBeInTheDocument();
    expect(publishWorkflowVersion).not.toHaveBeenCalled();
  });

  it('refuses to replace a newly changed current published version', async () => {
    const validDraft: WorkflowVersionDto = {
      ...version, validation_status: 'VALID',
      validation_report: { valid: true, content_hash: version.content_hash },
    };
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [validDraft], page: 1, page_size: 100, total: 1, has_next: false });
    vi.mocked(getWorkflowVersion).mockResolvedValue(validDraft);
    vi.mocked(getWorkflow)
      .mockResolvedValueOnce(workflow)
      .mockResolvedValueOnce({ ...workflow, current_version_id: '33333333-3333-4333-8333-333333333333' });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '게시' }));
    await user.click(screen.getByRole('button', { name: '게시 확정' }));
    expect(await screen.findByText(/게시 상태 또는 검증 결과가 변경되었습니다/)).toBeInTheDocument();
    expect(publishWorkflowVersion).not.toHaveBeenCalled();
  });

  it('reports backend publish refusal without claiming success', async () => {
    const validDraft: WorkflowVersionDto = {
      ...version, validation_status: 'VALID',
      validation_report: { valid: true, content_hash: version.content_hash },
    };
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [validDraft], page: 1, page_size: 100, total: 1, has_next: false });
    vi.mocked(getWorkflowVersion).mockResolvedValue(validDraft);
    vi.mocked(publishWorkflowVersion).mockRejectedValue(new Error('Backend rejected stale dependency'));
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '게시' }));
    await user.click(screen.getByRole('button', { name: '게시 확정' }));
    expect(await screen.findByText(/게시하지 못했습니다. 서버 검증 상태를 확인해 주세요/)).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(publishWorkflowVersion).toHaveBeenCalledTimes(1);
  });

  it('does not offer publish when there is only an immutable PUBLISHED version', async () => {
    vi.mocked(getWorkflow).mockResolvedValue({ ...workflow, status: 'ACTIVE', current_version_id: vId });
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [{ ...version, status: 'PUBLISHED', validation_status: 'VALID' }],
      page: 1, page_size: 100, total: 1, has_next: false,
    });
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    expect(await screen.findByRole('button', { name: '게시' })).toBeDisabled();
    expect(publishWorkflowVersion).not.toHaveBeenCalled();
  });

  it('activates a published valid workflow only after explicit confirmation', async () => {
    vi.mocked(getWorkflow).mockResolvedValue({ ...workflow, status: 'INACTIVE', current_version_id: vId });
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [publishedVersion], page: 1, page_size: 100, total: 1, has_next: false,
    });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '활성화' }));
    expect(changeWorkflowStatus).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '취소' }));
    expect(changeWorkflowStatus).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '활성화' }));
    await user.click(screen.getByRole('button', { name: '활성화 확정' }));
    await waitFor(() => expect(changeWorkflowStatus).toHaveBeenCalledWith(wfId, 'ACTIVE', 1));
    expect(await screen.findByRole('status')).toHaveTextContent('워크플로가 활성화되었습니다');
    expect(createWorkflowExecution).not.toHaveBeenCalled();
  });

  it('deactivates active workflows but refuses a stale lock_version', async () => {
    const active = { ...workflow, status: 'ACTIVE' as const, current_version_id: vId };
    vi.mocked(getWorkflow).mockResolvedValueOnce(active).mockResolvedValueOnce({ ...active, lock_version: 2 }).mockResolvedValue(active);
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [publishedVersion], page: 1, page_size: 100, total: 1, has_next: false,
    });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '비활성화' }));
    await user.click(screen.getByRole('button', { name: '비활성화 확정' }));
    expect(await screen.findByText(/다른 곳에서 변경되었습니다/)).toBeInTheDocument();
    expect(changeWorkflowStatus).not.toHaveBeenCalled();
  });

  it('runs only the exact ACTIVE published version with a stable idempotency key', async () => {
    const active = { ...workflow, status: 'ACTIVE' as const, current_version_id: vId };
    vi.mocked(getWorkflow).mockResolvedValue(active);
    vi.mocked(getWorkflowVersion).mockResolvedValue(publishedVersion);
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [publishedVersion], page: 1, page_size: 100, total: 1, has_next: false,
    });
    const user = userEvent.setup();
    const { router } = renderWithRouter(<WorkflowDetail />, {
      route: `/workflows/${wfId}`,
      routes: [
        { path: '/workflows/:workflowId', element: <WorkflowDetail /> },
        { path: '/executions/:executionId', element: <div>실행 상세</div> },
      ],
    });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    expect(createWorkflowExecution).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    await waitFor(() => expect(createWorkflowExecution).toHaveBeenCalledWith(wfId, vId, {}, expect.any(String)));
    expect(vi.mocked(createWorkflowExecution).mock.calls[0][3]).toMatch(/^[0-9a-f-]{36}$/i);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/executions/${execId}`));
  });

  it('refuses a run when the published version changed after the screen loaded', async () => {
    const active = { ...workflow, status: 'ACTIVE' as const, current_version_id: vId };
    vi.mocked(getWorkflow).mockResolvedValue(active);
    vi.mocked(getWorkflowVersion).mockResolvedValue({ ...publishedVersion, content_hash: 'changed-on-server' });
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [publishedVersion], page: 1, page_size: 100, total: 1, has_next: false,
    });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    expect(await screen.findByText(/워크플로 또는 게시 버전 상태가 변경되었습니다/)).toBeInTheDocument();
    expect(createWorkflowExecution).not.toHaveBeenCalled();
  });

  it('retries a failed manual run with the same idempotency key', async () => {
    vi.mocked(getWorkflow).mockResolvedValue({ ...workflow, status: 'ACTIVE', current_version_id: vId });
    vi.mocked(getWorkflowVersion).mockResolvedValue(publishedVersion);
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [publishedVersion], page: 1, page_size: 100, total: 1, has_next: false,
    });
    vi.mocked(createWorkflowExecution).mockRejectedValueOnce(new Error('temporary request failure'));
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, {
      route: `/workflows/${wfId}`,
      routes: [
        { path: '/workflows/:workflowId', element: <WorkflowDetail /> },
        { path: '/executions/:executionId', element: <div>실행 상세</div> },
      ],
    });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    expect(await screen.findByText(/동일한 요청 키로 재시도할 수 있습니다/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    await waitFor(() => expect(createWorkflowExecution).toHaveBeenCalledTimes(2));
    const calls = vi.mocked(createWorkflowExecution).mock.calls;
    expect(calls[0][3]).toBe(calls[1][3]);
  });

  it('keeps the run confirmation open on invalid input and never POSTs', async () => {
    vi.mocked(getWorkflow).mockResolvedValue({ ...workflow, status: 'ACTIVE', current_version_id: vId });
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [{ ...publishedVersion, plan_definition: { ...publishedVersion.plan_definition,
        inputs: { report_name: { type: 'string', required: true, secret: false } },
      } }], page: 1, page_size: 100, total: 1, has_next: false,
    });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    expect(await screen.findByText(/필수 입력값이 없습니다: report_name/)).toBeInTheDocument();
    expect(createWorkflowExecution).not.toHaveBeenCalled();
  });

  it('renders typed form inputs and submits the parsed values via the existing execution API', async () => {
    const active = { ...workflow, status: 'ACTIVE' as const, current_version_id: vId };
    const withInputs = { ...publishedVersion, plan_definition: {
      ...publishedVersion.plan_definition,
      inputs: {
        title: { type: 'string', required: true, secret: false },
        count: { type: 'integer', required: true, secret: false },
        dry_run: { type: 'boolean', required: true, secret: false },
        optional_note: { type: 'string', required: false, secret: false },
      },
    } };
    vi.mocked(getWorkflow).mockResolvedValue(active);
    vi.mocked(getWorkflowVersion).mockResolvedValue(withInputs);
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [withInputs], page: 1, page_size: 100, total: 1, has_next: false });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, {
      route: `/workflows/${wfId}`,
      routes: [
        { path: '/workflows/:workflowId', element: <WorkflowDetail /> },
        { path: '/executions/:executionId', element: <div>실행 상세</div> },
      ],
    });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    expect(screen.queryByRole('textbox', { name: '입력값 JSON 객체' })).not.toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: 'title' }), '일일 보고');
    await user.type(screen.getByRole('textbox', { name: 'count' }), '2');
    await user.selectOptions(screen.getByRole('combobox', { name: 'dry_run' }), 'false');
    await user.click(screen.getByRole('checkbox', { name: 'optional_note 입력 사용' }));
    await user.type(screen.getByRole('textbox', { name: 'optional_note' }), '메모');
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    await waitFor(() => expect(createWorkflowExecution).toHaveBeenCalledWith(wfId, vId, {
      title: '일일 보고', count: 2, dry_run: false, optional_note: '메모',
    }, expect.any(String)));
  });

  it('preserves values when toggling between the form and advanced JSON mode', async () => {
    const withInputs = { ...publishedVersion, plan_definition: {
      ...publishedVersion.plan_definition, inputs: {
        title: { type: 'string', required: true, secret: false },
      },
    } };
    vi.mocked(getWorkflow).mockResolvedValue({ ...workflow, status: 'ACTIVE', current_version_id: vId });
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [withInputs], page: 1, page_size: 100, total: 1, has_next: false });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    await user.type(screen.getByRole('textbox', { name: 'title' }), '기존 값');
    await user.click(screen.getByRole('button', { name: '고급 JSON 입력' }));
    const jsonInput = screen.getByRole('textbox', { name: '입력값 JSON 객체' }) as HTMLTextAreaElement;
    expect(JSON.parse(jsonInput.value)).toEqual({ title: '기존 값' });
    fireEvent.change(jsonInput, { target: { value: '{"title":"JSON 수정"}' } });
    await user.click(screen.getByRole('button', { name: '폼 입력으로 돌아가기' }));
    expect(screen.getByRole('textbox', { name: 'title' })).toHaveValue('JSON 수정');
    expect(createWorkflowExecution).not.toHaveBeenCalled();
  });

  it('blocks invalid typed input without creating an execution', async () => {
    const withInputs = { ...publishedVersion, plan_definition: {
      ...publishedVersion.plan_definition, inputs: {
        count: { type: 'integer', required: true, secret: false },
        credential: { type: 'string', required: false, secret: true },
      },
    } };
    vi.mocked(getWorkflow).mockResolvedValue({ ...workflow, status: 'ACTIVE', current_version_id: vId });
    vi.mocked(listWorkflowVersions).mockResolvedValue({ items: [withInputs], page: 1, page_size: 100, total: 1, has_next: false });
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    await user.type(screen.getByRole('textbox', { name: 'count' }), '1.2');
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    expect(await screen.findByText(/count: 올바른 정수/)).toBeInTheDocument();
    expect(createWorkflowExecution).not.toHaveBeenCalled();
    await user.clear(screen.getByRole('textbox', { name: 'count' }));
    await user.type(screen.getByRole('textbox', { name: 'count' }), '2');
    await user.click(screen.getByRole('checkbox', { name: 'credential 입력 사용' }));
    await user.type(screen.getByRole('textbox', { name: 'credential' }), 'plaintext');
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    expect(await screen.findByText(/credential: 등록된 Secret의 UUID/)).toBeInTheDocument();
    expect(createWorkflowExecution).not.toHaveBeenCalled();
  });

  it('never enables a manual run for inactive or invalid published workflows', async () => {
    vi.mocked(getWorkflow).mockResolvedValue({ ...workflow, status: 'INACTIVE', current_version_id: vId });
    vi.mocked(listWorkflowVersions).mockResolvedValue({
      items: [{ ...publishedVersion, validation_status: 'INVALID' }],
      page: 1, page_size: 100, total: 1, has_next: false,
    });
    renderWithRouter(<WorkflowDetail />, { path: '/workflows/:workflowId', route: `/workflows/${wfId}` });
    expect(await screen.findByRole('button', { name: '수동 실행' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '활성화' })).toBeDisabled();
    expect(createWorkflowExecution).not.toHaveBeenCalled();
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
