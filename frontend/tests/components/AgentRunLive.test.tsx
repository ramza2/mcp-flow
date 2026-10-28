// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import AgentRunLive from '@/screens/work/AgentRunLive';
import { renderWithRouter } from '../test-utils';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  submit: vi.fn(),
  analyze: vi.fn(),
  latest: vi.fn(),
  getSelection: vi.fn(),
  select: vi.fn(),
  progress: vi.fn(),
  build: vi.fn(),
  generate: vi.fn(),
  validate: vi.fn(),
  respond: vi.fn(),
  createExecution: vi.fn(),
  listExecutions: vi.fn(),
  chatResult: vi.fn(),
  getExecution: vi.fn(),
  listExecutionSteps: vi.fn(),
}));

vi.mock('@/api/agentRun', () => ({
  listRunnableAgents: mocks.list,
  submitAgentRun: mocks.submit,
  analyzeAgentRun: mocks.analyze,
  getConversationLatestAgentRun: mocks.latest,
  getAgentRunSelection: mocks.getSelection,
  selectToolForAgentRun: mocks.select,
  getAgentRunProgress: mocks.progress,
  buildAgentRunParameters: mocks.build,
  generateAgentRunPlan: mocks.generate,
  validateAgentRunPlan: mocks.validate,
  respondToAgentRunClarification: mocks.respond,
  createAgentRunExecution: mocks.createExecution,
  getAgentRunChatResult: mocks.chatResult,
}));

vi.mock('@/api/executions', () => ({
  listExecutions: mocks.listExecutions,
  getExecution: mocks.getExecution,
  listExecutionSteps: mocks.listExecutionSteps,
}));

const sseMocks = vi.hoisted(() => ({
  onEvent: null as null | ((message: {
    sseId: string;
    envelope: { event_type: string; event_id: string; execution_id: string; step_execution_id: null; payload: Record<string, never>; payload_version: number; occurred_at: string };
  }) => void),
  available: false,
}));

vi.mock('@/api/executionEvents', () => ({
  isEventSourceAvailable: () => sseMocks.available,
  openExecutionEventsStream: (options: { onEvent: typeof sseMocks.onEvent }) => {
    sseMocks.onEvent = options.onEvent;
    return {
      close: () => {
        sseMocks.onEvent = null;
      },
    };
  },
}));

const conversationId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const requestId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const agentId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const toolVersionId = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
const executionId = '99999999-9999-4999-8999-999999999999';

function row(status: string, structured: object | null = null) {
  return {
    id: requestId,
    conversation_id: conversationId,
    agent_version_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
    status,
    raw_request_text: '문서를 검색해줘',
    structured_request: structured,
    rejection_code: null,
    created_at: '2026-10-23T10:00:00Z',
    analyzed_at: status === 'RECEIVED' ? null : '2026-10-23T10:00:01Z',
  };
}

function selection(decision: 'AUTO_SELECT' | 'NO_MATCH' = 'AUTO_SELECT') {
  return {
    id: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
    decision,
    selected_tool_version_id: decision === 'AUTO_SELECT' ? toolVersionId : null,
    selected_tool_name: decision === 'AUTO_SELECT' ? '날씨 조회' : null,
    confidence: decision === 'AUTO_SELECT' ? 0.92 : null,
    reason_summary: null,
    created_at: '2026-10-23T10:00:02Z',
  };
}

function stage(status: string, overrides: Record<string, unknown> = {}) {
  return {
    request: row(status, { intent: 'Find documents', required_capabilities: ['document.lookup'] }),
    parameter_build: null,
    plan: null,
    validation: null,
    open_clarification: null,
    safe_auto_execute: false,
    ...overrides,
  };
}

function renderLive(path = '/run') {
  return renderWithRouter(<AgentRunLive />, {
    route: path, path: '/run',
    routes: [
      { path: '/run', element: <AgentRunLive /> },
      { path: '/run/:conversationId', element: <AgentRunLive /> },
      { path: '/executions', element: <div>실제 실행 목록</div> },
      { path: '/executions/:executionId', element: <div>실제 실행 상세</div> },
    ],
  });
}

describe('AgentRunLive — chat phase 1', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    sseMocks.available = false;
    sseMocks.onEvent = null;
    mocks.list.mockResolvedValue({
      items: [{ id: agentId, agent_version_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', name: '실제 Agent', description: null, version_no: 2 }],
    });
    mocks.submit.mockResolvedValue(row('RECEIVED'));
    mocks.listExecutions.mockResolvedValue({ items: [], page: 1, page_size: 1, total: 0 });
    mocks.createExecution.mockResolvedValue({
      id: executionId, status: 'CREATED', source_type: 'AGENT_REQUEST', trigger_type: 'USER',
      agent_request_id: requestId, agent_version_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
      plan_hash: 'a'.repeat(64), requested_at: '2026-10-23T10:05:00Z', step_count: 1,
    });
    mocks.chatResult.mockResolvedValue({
      agent_request_id: requestId,
      execution_id: executionId,
      execution_status: 'SUCCEEDED',
      available: true,
      unavailable_reason: null,
      tool_name: 'GET /pets',
      display_items: [
        { name: 'Alpha', status: 'available' },
        { name: 'Beta', status: 'pending' },
      ],
      summary_text: 'GET /pets 결과 2건을 가져왔습니다.',
      truncated: false,
      steps: [
        { step_key: 'tool_1', status: 'SUCCEEDED', tool_name: 'GET /pets' },
      ],
    });
    mocks.listExecutionSteps.mockResolvedValue({
      items: [
        {
          id: 'step-1',
          execution_id: executionId,
          step_key: 'tool_1',
          step_type: 'TOOL',
          parent_step_id: null,
          sequence_hint: 0,
          mcp_tool_version_id: toolVersionId,
          iteration_no: null,
          status: 'SUCCEEDED',
          attempt_count: 1,
          condition_result: null,
          ready_at: null,
          started_at: null,
          finished_at: null,
          error_code: null,
          error_category: null,
          duration_ms: null,
        },
      ],
    });
    mocks.getExecution.mockResolvedValue({
      id: executionId, status: 'SUCCEEDED', agent_request_id: requestId,
    });
    mocks.analyze.mockResolvedValue(row('RETRIEVING', {
      intent: 'Find documents',
      required_capabilities: ['document.lookup'],
      entities: [],
      risk_hints: ['READ_ONLY'],
      needs_clarification: false,
      missing_inputs: [],
      ambiguities: [],
    }));
    mocks.latest.mockResolvedValue(row('RETRIEVING', {
      intent: 'Find documents', required_capabilities: ['document.lookup'],
    }));
    mocks.getSelection.mockResolvedValue({
      request: row('RETRIEVING', {
        intent: 'Find documents', required_capabilities: ['document.lookup'],
      }),
      selection: null,
    });
    mocks.progress.mockResolvedValue(stage('RETRIEVING'));
    mocks.build.mockResolvedValue(stage('PLANNING', {
      parameter_build: { id: 'aaaa', tool_version_id: toolVersionId, is_complete: true, missing_fields: [], created_at: '2026-10-23T10:01:00Z' },
    }));
    mocks.generate.mockResolvedValue(stage('VALIDATING', {
      plan: { id: 'bbbb', plan_hash: 'a'.repeat(64), goal: '문서 검색', step_count: 1, step_names: ['문서 조회'], created_at: '2026-10-23T10:02:00Z' },
    }));
    mocks.validate.mockResolvedValue(stage('READY', {
      validation: { id: 'cccc', decision: 'READY', confirmation_required: false, error_count: 0, warning_count: 0, created_at: '2026-10-23T10:03:00Z' },
      plan: { id: 'bbbb', plan_hash: 'a'.repeat(64), goal: '문서 검색', step_count: 1, step_names: ['문서 조회'], created_at: '2026-10-23T10:02:00Z' },
      safe_auto_execute: true,
    }));
    mocks.respond.mockResolvedValue({ agent_request_id: requestId, clarification_id: 'ddd', clarification_status: 'ANSWERED', agent_request_status: 'VALIDATING' });
    mocks.select.mockResolvedValue({
      request: row('BUILDING_PARAMETERS', {
        intent: 'Find documents', required_capabilities: ['document.lookup'],
      }),
      selection: selection(),
    });
  });

  it('shows Korean chat shell with execute button', async () => {
    renderLive();
    expect(await screen.findByRole('heading', { name: /Agent 업무 실행/ })).toBeTruthy();
    expect(screen.getByPlaceholderText(/무엇을 도와드릴까요/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '실행' })).toBeDisabled();
  });

  it('auto-chains planning APIs for a safe READ_ONLY path and starts Execution', async () => {
    // progress is called repeatedly during auto pipeline — return evolving stages.
    mocks.progress
      .mockResolvedValueOnce(stage('RETRIEVING'))
      .mockResolvedValueOnce(stage('BUILDING_PARAMETERS'))
      .mockResolvedValueOnce(stage('PLANNING', {
        parameter_build: { id: 'aaaa', tool_version_id: toolVersionId, is_complete: true, missing_fields: [], created_at: '2026-10-23T10:01:00Z' },
      }))
      .mockResolvedValue(stage('READY', {
        validation: { id: 'cccc', decision: 'READY', confirmation_required: false, error_count: 0, warning_count: 0, created_at: '2026-10-23T10:03:00Z' },
        plan: { id: 'bbbb', plan_hash: 'a'.repeat(64), goal: '문서 검색', step_count: 1, step_names: ['문서 조회'], created_at: '2026-10-23T10:02:00Z' },
        safe_auto_execute: true,
      }));

    // After select → BUILDING_PARAMETERS; build → PLANNING; generate → VALIDATING; validate → READY+safe
    mocks.select.mockResolvedValue({
      request: row('BUILDING_PARAMETERS'),
      selection: selection(),
    });
    mocks.build.mockResolvedValue(stage('PLANNING', {
      parameter_build: { id: 'aaaa', tool_version_id: toolVersionId, is_complete: true, missing_fields: [], created_at: '2026-10-23T10:01:00Z' },
    }));
    mocks.generate.mockResolvedValue(stage('VALIDATING', {
      plan: { id: 'bbbb', plan_hash: 'a'.repeat(64), goal: '문서 검색', step_count: 1, step_names: ['문서 조회'], created_at: '2026-10-23T10:02:00Z' },
    }));
    mocks.validate.mockResolvedValue(stage('READY', {
      validation: { id: 'cccc', decision: 'READY', confirmation_required: false, error_count: 0, warning_count: 0, created_at: '2026-10-23T10:03:00Z' },
      plan: { id: 'bbbb', plan_hash: 'a'.repeat(64), goal: '문서 검색', step_count: 1, step_names: ['문서 조회'], created_at: '2026-10-23T10:02:00Z' },
      safe_auto_execute: true,
    }));

    renderLive();
    await screen.findByRole('option', { name: '실제 Agent (v2)' });
    fireEvent.change(screen.getByPlaceholderText(/무엇을 도와드릴까요/), {
      target: { value: '반려동물 목록을 조회해 줘' },
    });
    fireEvent.click(screen.getByRole('button', { name: '실행' }));

    await waitFor(() => {
      expect(mocks.submit).toHaveBeenCalledWith(agentId, '반려동물 목록을 조회해 줘');
      expect(mocks.analyze).toHaveBeenCalledWith(requestId);
      expect(mocks.select).toHaveBeenCalledWith(requestId);
      expect(mocks.build).toHaveBeenCalledWith(requestId);
      expect(mocks.generate).toHaveBeenCalledWith(requestId);
      expect(mocks.validate).toHaveBeenCalledWith(requestId);
      expect(mocks.createExecution).toHaveBeenCalledWith(requestId);
    });
    expect(screen.queryByText(/메일을 발송했습니다/)).toBeNull();
    // Stay on chat — do not navigate away to execution detail exclusively.
    expect(screen.queryByText('실제 실행 상세')).toBeNull();
  });

  it('does not auto-execute when safe_auto_execute is false', async () => {
    mocks.latest.mockResolvedValue(row('READY'));
    mocks.getSelection.mockResolvedValue({ request: row('READY'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('READY', {
      plan: { id: 'saved-plan', plan_hash: 'b'.repeat(64), goal: '저장된 계획', step_count: 1, step_names: ['도구 조회'], created_at: '2026-10-23T10:02:00Z' },
      validation: { id: 'saved-validation', decision: 'READY', confirmation_required: false, error_count: 0, warning_count: 0, created_at: '2026-10-23T10:03:00Z' },
      safe_auto_execute: false,
    }));
    renderLive(`/run/${conversationId}`);
    expect(await screen.findByRole('button', { name: '실제 업무 실행 시작' })).toBeDisabled();
    expect(mocks.createExecution).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox', {
      name: '실제 시스템에서 실행되는 작업임을 확인했습니다.',
    }));
    fireEvent.click(screen.getByRole('button', { name: '실제 업무 실행 시작' }));
    await waitFor(() => expect(mocks.createExecution).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('실제 실행 상세')).toBeNull();
  });

  it('does not auto-retry persisted ANALYZING after reload', async () => {
    mocks.latest.mockResolvedValue(row('ANALYZING'));
    mocks.getSelection.mockResolvedValue({
      request: row('ANALYZING'), selection: null,
    });
    mocks.progress.mockResolvedValue(stage('ANALYZING'));
    renderLive(`/run/${conversationId}`);
    fireEvent.click(await screen.findByRole('button', { name: '고급 보기' }));
    expect(await screen.findByText(/분석이 진행 중이거나 중단됐을 수 있습니다/)).toBeTruthy();
    expect(mocks.analyze).not.toHaveBeenCalled();
  });

  it('shows clarification without starting Execution', async () => {
    mocks.latest.mockResolvedValue(row('WAITING_INPUT'));
    mocks.getSelection.mockResolvedValue({ request: row('WAITING_INPUT'), selection: null });
    mocks.progress.mockResolvedValue(stage('WAITING_INPUT', {
      open_clarification: {
        id: 'clarify-2', request_type: 'MISSING_PARAMETER',
        prompt_text: '추가 입력이 필요합니다: city',
        fields: [
          { name: 'city', value_type: 'string', description: null, required: true },
        ],
        requested_at: '2026-10-23T10:04:00Z',
      },
    }));
    renderLive(`/run/${conversationId}`);
    expect(await screen.findByText(/추가 입력이 필요합니다/)).toBeTruthy();
    fireEvent.change(await screen.findByLabelText('city (필수)'), { target: { value: '서울' } });
    fireEvent.click(screen.getByRole('button', { name: '입력 내용 제출' }));
    await waitFor(() => expect(mocks.respond).toHaveBeenCalledWith(
      requestId, 'clarify-2', { city: '서울' },
    ));
    expect(mocks.createExecution).not.toHaveBeenCalled();
  });

  it('restores an existing execution on F5 and loads chat result when terminal', async () => {
    mocks.latest.mockResolvedValue(row('READY'));
    mocks.getSelection.mockResolvedValue({ request: row('READY'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('READY', { safe_auto_execute: true }));
    mocks.listExecutions.mockResolvedValue({
      items: [{ id: executionId, status: 'SUCCEEDED', agent_request_id: requestId }],
      page: 1, page_size: 1, total: 1,
    });
    renderLive(`/run/${conversationId}`);
    await waitFor(() => {
      expect(mocks.chatResult).toHaveBeenCalled();
      // Results are announced once in chat (no duplicate result card).
      expect(screen.getByText(/· Alpha \/ available/)).toBeTruthy();
      expect(screen.getByText(/· Beta \/ pending/)).toBeTruthy();
    });
    expect(screen.getAllByText(/결과 2건을 가져왔습니다/).length).toBe(1);
    expect(mocks.createExecution).not.toHaveBeenCalled();
  });

  it('does not duplicate the final answer when SSE and polling both complete', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    sseMocks.available = true;
    mocks.latest.mockResolvedValue(row('READY'));
    mocks.getSelection.mockResolvedValue({ request: row('READY'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('READY', { safe_auto_execute: true }));
    mocks.listExecutions.mockResolvedValue({
      items: [{ id: executionId, status: 'RUNNING', agent_request_id: requestId }],
      page: 1, page_size: 1, total: 1,
    });
    mocks.getExecution.mockResolvedValue({
      id: executionId, status: 'SUCCEEDED', agent_request_id: requestId,
    });
    renderLive(`/run/${conversationId}`);
    await waitFor(() => expect(sseMocks.onEvent).toBeTruthy());

    sseMocks.onEvent?.({
      sseId: '1',
      envelope: {
        event_id: 'evt-1',
        execution_id: executionId,
        step_execution_id: null,
        event_type: 'execution.succeeded',
        payload: {},
        payload_version: 1,
        occurred_at: '2026-10-23T10:06:00Z',
      },
    });
    // Polling fallback also observes SUCCEEDED for the same execution.
    await vi.advanceTimersByTimeAsync(2600);

    await waitFor(() => {
      expect(screen.getByText(/· Alpha \/ available/)).toBeTruthy();
      expect(screen.getByText(/· Beta \/ pending/)).toBeTruthy();
    });
    expect(screen.getAllByText(/결과 2건을 가져왔습니다/).length).toBe(1);
    expect(mocks.chatResult.mock.calls.length).toBeGreaterThanOrEqual(1);
    vi.useRealTimers();
  });

  it('does not auto-retry ambiguous Execution creation errors', async () => {
    mocks.latest.mockResolvedValue(row('READY'));
    mocks.getSelection.mockResolvedValue({ request: row('READY'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('READY', { safe_auto_execute: false }));
    mocks.createExecution.mockRejectedValue(new Error('Network timeout'));
    renderLive(`/run/${conversationId}`);

    const start = await screen.findByRole('button', { name: '실제 업무 실행 시작' });
    fireEvent.click(screen.getByRole('checkbox', {
      name: '실제 시스템에서 실행되는 작업임을 확인했습니다.',
    }));
    fireEvent.click(start);
    expect((await screen.findByRole('alert')).textContent).toContain('자동 재시도하지 않습니다');
    expect(mocks.createExecution).toHaveBeenCalledTimes(1);
  });

  it('keeps advanced view for explicit Tool selection steps', async () => {
    // Prevent auto-pipeline from racing: status RETRIEVING but mark auto-started by
    // returning ANALYZING first... actually use SELECTING stuck path.
    mocks.latest.mockResolvedValue(row('RETRIEVING'));
    mocks.getSelection.mockResolvedValue({ request: row('RETRIEVING'), selection: null });
    // Auto pipeline will call select — that is intended for chat. For advanced manual
    // button, open advanced after pausing auto by using a status the auto loop stops on.
    mocks.progress.mockResolvedValue(stage('RETRIEVING'));
    mocks.select.mockResolvedValue({
      request: row('BUILDING_PARAMETERS'),
      selection: selection(),
    });
    renderLive(`/run/${conversationId}`);
    // Auto pipeline may already call select; wait then open advanced.
    await waitFor(() => expect(mocks.select).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: '고급 보기' }));
    expect(await screen.findByText(/선택 Tool: 날씨 조회/)).toBeTruthy();
    expect(screen.getByText(/MCP 호출 또는 업무 실행 완료를 의미하지 않습니다/)).toBeTruthy();
  });

  it('does not allow submission without an eligible real agent', async () => {
    mocks.list.mockResolvedValue({ items: [] });
    renderLive();
    expect(await screen.findByText('사용 가능한 Agent 없음')).toBeTruthy();
    expect(screen.getByRole('button', { name: '실행' })).toBeDisabled();
    expect(mocks.submit).not.toHaveBeenCalled();
  });
});
