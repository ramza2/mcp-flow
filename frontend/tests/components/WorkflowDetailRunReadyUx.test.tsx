// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WorkflowDetail from '@/screens/build/WorkflowDetail';
import { ApiError } from '@/api/client';
import {
  activateDisabledReason,
  buildWorkflowRunReadySteps,
  formatWorkflowRunSubmitError,
  nextWorkflowRunReadyAction,
  publishDisabledReason,
  runDisabledReason,
  WORKFLOW_RUN_READY_FLOW_LABEL,
} from '@/screens/build/workflowRunReadyUx';
import { renderWithRouter } from '../test-utils';

const mocks = vi.hoisted(() => ({
  getWorkflow: vi.fn(),
  listWorkflowVersions: vi.fn(),
  getWorkflowVersion: vi.fn(),
  createWorkflowVersion: vi.fn(),
  publishWorkflowVersion: vi.fn(),
  changeWorkflowStatus: vi.fn(),
  createWorkflowExecution: vi.fn(),
}));

vi.mock('@/api/workflows', () => ({
  getWorkflow: mocks.getWorkflow,
  listWorkflowVersions: mocks.listWorkflowVersions,
  getWorkflowVersion: mocks.getWorkflowVersion,
  createWorkflowVersion: mocks.createWorkflowVersion,
  publishWorkflowVersion: mocks.publishWorkflowVersion,
  changeWorkflowStatus: mocks.changeWorkflowStatus,
  createWorkflowExecution: mocks.createWorkflowExecution,
}));

const wfId = '11111111-1111-4111-8111-111111111111';
const vId = '22222222-2222-4222-8222-222222222222';

const workflow = {
  id: wfId,
  name: '업무 자동화',
  description: '설명',
  status: 'DRAFT' as const,
  visibility: 'PRIVATE' as const,
  owner_id: '33333333-3333-4333-8333-333333333333',
  current_version_id: null as string | null,
  lock_version: 1,
  created_at: '2026-11-18T00:00:00.000Z',
  updated_at: '2026-11-18T00:00:00.000Z',
};

const draftVersion = {
  id: vId,
  workflow_id: wfId,
  version_no: 1,
  status: 'DRAFT' as const,
  validation_status: 'INVALID' as const,
  validation_report: { valid: false, content_hash: 'hash-1', issues: [] },
  content_hash: 'hash-1',
  change_summary: '초안',
  plan_definition: {
    schema_version: '1.0',
    goal: 'g',
    source: { type: 'WORKFLOW', workflow_id: wfId },
    inputs: {},
    limits: { max_steps: 20, max_duration_seconds: 300, max_parallelism: 4, max_loop_iterations: 50 },
    steps: [],
    completion: { success_policy: 'ALL_REQUIRED', response_step_ids: [] },
  },
  created_at: '2026-11-18T00:00:00.000Z',
  updated_at: '2026-11-18T00:00:00.000Z',
};

beforeEach(() => {
  Object.values(mocks).forEach((fn) => fn.mockReset());
  mocks.getWorkflow.mockResolvedValue(workflow);
  mocks.listWorkflowVersions.mockResolvedValue({
    items: [draftVersion],
    page: 1,
    page_size: 100,
    total: 1,
    has_next: false,
  });
  mocks.getWorkflowVersion.mockResolvedValue(draftVersion);
  mocks.createWorkflowExecution.mockResolvedValue({ id: '44444444-4444-4444-8444-444444444444' });
});

afterEach(() => vi.restoreAllMocks());

describe('workflowRunReadyUx helpers', () => {
  it('guides draft → validate → publish → activate → run without inventing Domain values', () => {
    expect(WORKFLOW_RUN_READY_FLOW_LABEL).toMatch(/초안 → 검증 → 게시 → 활성화 → 실행 가능/);
    const early = buildWorkflowRunReadySteps({
      hasDraft: true,
      draftValidated: false,
      hasPublishedValid: false,
      isActive: false,
    });
    expect(nextWorkflowRunReadyAction(early)?.stepId).toBe('validate');
    expect(publishDisabledReason({ busy: false, hasDraft: true, canPublish: false })).toMatch(
      /서버 검증/,
    );
    expect(
      activateDisabledReason({ busy: false, workflowStatus: 'DRAFT', canActivate: false }),
    ).toMatch(/게시 버전/);
    expect(
      runDisabledReason({
        busy: false,
        canRun: false,
        workflowStatus: 'DRAFT',
        hasPublishedValid: false,
      }),
    ).toMatch(/ACTIVE/);
  });

  it('marks ACTIVE+published as runnable ready, never as execution completed', () => {
    const ready = buildWorkflowRunReadySteps({
      hasDraft: false,
      draftValidated: false,
      hasPublishedValid: true,
      isActive: true,
    });
    const runStep = ready.find((step) => step.id === 'run');
    expect(runStep?.done).toBe(false);
    expect(runStep?.ready).toBe(true);
    expect(runStep?.label).toMatch(/실행 가능/);
    const next = nextWorkflowRunReadyAction(ready);
    expect(next?.stepId).toBe('run');
    expect(next?.message).toMatch(/실행 가능/);
    expect(next?.message).toMatch(/완료로 추정하지/);
    expect(next?.message).not.toMatch(/실행 완료|실행이 끝났/);
  });

  it('formats run submit errors for 403, conflict, and uncertain 5xx/network', () => {
    expect(
      formatWorkflowRunSubmitError({
        status: 403,
        message: 'Missing workflow.execute',
      }),
    ).toMatch(/workflow\.execute/);
    expect(
      formatWorkflowRunSubmitError({
        status: 409,
        message: 'Version conflict',
      }),
    ).toMatch(/다시 확인/);
    const network = formatWorkflowRunSubmitError({
      status: 0,
      code: 'NETWORK_ERROR',
      message: 'Network request failed',
    });
    expect(network).toMatch(/실행 목록에서 먼저 확인/);
    expect(network).toMatch(/성공으로 표시하지/);
    expect(network).toMatch(/무조건 재시도하지/);
    expect(network).not.toMatch(/실행이 생성되었습니다/);
    const serverError = formatWorkflowRunSubmitError({
      status: 500,
      message: 'Internal Server Error',
    });
    expect(serverError).toContain('Internal Server Error');
    expect(serverError).toMatch(/실행 목록에서 먼저 확인/);
    expect(serverError).toMatch(/무조건 재시도하지/);
    expect(serverError).not.toMatch(/동일한 요청 키로 재시도할 수 있습니다/);
    expect(
      formatWorkflowRunSubmitError({ status: 422, message: 'invalid input' }),
    ).toMatch(/동일한 요청 키/);
  });
});

describe('WorkflowDetail run-ready banners', () => {
  it('shows readiness progress and publish disabled reason in the body', async () => {
    renderWithRouter(<WorkflowDetail />, {
      path: '/workflows/:workflowId',
      route: `/workflows/${wfId}`,
    });
    expect(await screen.findByTestId('workflow-run-ready-progress')).toHaveTextContent(
      WORKFLOW_RUN_READY_FLOW_LABEL,
    );
    expect(screen.getByTestId('workflow-run-ready-next')).toHaveTextContent(/검증/);
    expect(screen.getByTestId('workflow-run-ready-disabled-reasons')).toHaveTextContent(
      /게시 비활성/,
    );
    expect(screen.getByRole('button', { name: '게시' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '수동 실행' })).toBeDisabled();
  });

  it('keeps form/idempotency run path and surfaces network uncertainty without claiming success', async () => {
    const published = {
      ...draftVersion,
      status: 'PUBLISHED' as const,
      validation_status: 'VALID' as const,
      validation_report: { valid: true, content_hash: 'hash-1', issues: [] },
    };
    const active = {
      ...workflow,
      status: 'ACTIVE' as const,
      current_version_id: vId,
    };
    mocks.getWorkflow.mockResolvedValue(active);
    mocks.getWorkflowVersion.mockResolvedValue(published);
    mocks.listWorkflowVersions.mockResolvedValue({
      items: [published],
      page: 1,
      page_size: 100,
      total: 1,
      has_next: false,
    });
    mocks.createWorkflowExecution.mockRejectedValue(
      new ApiError({
        status: 0,
        code: 'NETWORK_ERROR',
        message: 'Network request failed',
      }),
    );
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, {
      path: '/workflows/:workflowId',
      route: `/workflows/${wfId}`,
    });
    expect(await screen.findByTestId('workflow-run-ready-step-run')).toHaveAttribute(
      'data-ready',
      'true',
    );
    expect(screen.getByTestId('workflow-run-ready-step-run')).toHaveAttribute(
      'data-done',
      'false',
    );
    expect(screen.getByTestId('workflow-run-ready-next')).toHaveTextContent(/실행 가능/);
    expect(screen.getByTestId('workflow-run-ready-next')).toHaveTextContent(/완료로 추정하지/);
    await user.click(screen.getByRole('button', { name: '수동 실행' }));
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    expect(await screen.findByText(/실행 목록에서 먼저 확인/)).toBeInTheDocument();
    expect(screen.getByText(/무조건 재시도하지/)).toBeInTheDocument();
    expect(screen.queryByText(/실행이 생성되었습니다/)).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.createWorkflowExecution).toHaveBeenCalledTimes(1));
    expect(mocks.createWorkflowExecution.mock.calls[0][3]).toMatch(/^[0-9a-f-]{36}$/i);
  });

  it('on 5xx keeps idempotency key but prioritizes execution-history check over retry', async () => {
    const published = {
      ...draftVersion,
      status: 'PUBLISHED' as const,
      validation_status: 'VALID' as const,
      validation_report: { valid: true, content_hash: 'hash-1', issues: [] },
    };
    mocks.getWorkflow.mockResolvedValue({
      ...workflow,
      status: 'ACTIVE' as const,
      current_version_id: vId,
    });
    mocks.getWorkflowVersion.mockResolvedValue(published);
    mocks.listWorkflowVersions.mockResolvedValue({
      items: [published],
      page: 1,
      page_size: 100,
      total: 1,
      has_next: false,
    });
    mocks.createWorkflowExecution.mockRejectedValue(
      new ApiError({ status: 503, code: 'UNAVAILABLE', message: 'Service Unavailable' }),
    );
    const user = userEvent.setup();
    renderWithRouter(<WorkflowDetail />, {
      path: '/workflows/:workflowId',
      route: `/workflows/${wfId}`,
    });
    await user.click(await screen.findByRole('button', { name: '수동 실행' }));
    await user.click(screen.getByRole('button', { name: '실행 확정' }));
    expect(await screen.findByText(/Service Unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/실행 목록에서 먼저 확인/)).toBeInTheDocument();
    expect(screen.queryByText(/동일한 요청 키로 재시도할 수 있습니다/)).not.toBeInTheDocument();
    expect(mocks.createWorkflowExecution.mock.calls[0][3]).toMatch(/^[0-9a-f-]{36}$/i);
  });
});
