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
}));

const conversationId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const requestId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const agentId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const toolVersionId = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';

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
    ],
  });
}

describe('AgentRunLive — real phase 1, no fake execution', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.list.mockResolvedValue({
      items: [{ id: agentId, agent_version_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', name: '실제 Agent', description: null, version_no: 2 }],
    });
    mocks.submit.mockResolvedValue(row('RECEIVED'));
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
    }));
    mocks.respond.mockResolvedValue({ agent_request_id: requestId, clarification_id: 'ddd', clarification_status: 'ANSWERED', agent_request_status: 'VALIDATING' });
    mocks.select.mockResolvedValue({
      request: row('BUILDING_PARAMETERS', {
        intent: 'Find documents', required_capabilities: ['document.lookup'],
      }),
      selection: selection(),
    });
  });

  it('submits once, invokes real analyzer once, and never claims Tool/Execution completed', async () => {
    renderLive();
    await screen.findByRole('option', { name: '실제 Agent (v2)' });
    fireEvent.change(screen.getByPlaceholderText('분석할 자연어 업무 지시를 입력하세요.'), {
      target: { value: '문서를 검색해줘' },
    });
    fireEvent.click(screen.getByRole('button', { name: '실제 분석 시작' }));

    await waitFor(() => {
      expect(mocks.submit).toHaveBeenCalledWith(agentId, '문서를 검색해줘');
      expect(mocks.analyze).toHaveBeenCalledWith(requestId);
    });
    expect(await screen.findByText('Find documents')).toBeTruthy();
    expect(screen.getByText('Execution: 생성되지 않음')).toBeTruthy();
    expect(screen.queryByText(/메일을 발송했습니다/)).toBeNull();
    expect(mocks.submit).toHaveBeenCalledTimes(1);
    expect(mocks.analyze).toHaveBeenCalledTimes(1);
  });

  it('reloads a persisted request without retrying the model', async () => {
    renderLive(`/run/${conversationId}`);
    expect(await screen.findByText('Find documents')).toBeTruthy();
    expect(mocks.latest).toHaveBeenCalledWith(conversationId, expect.anything());
    expect(mocks.analyze).not.toHaveBeenCalled();
  });

  it('does not allow submission without an eligible real agent', async () => {
    mocks.list.mockResolvedValue({ items: [] });
    renderLive();
    expect(await screen.findByText('사용 가능한 Agent 없음')).toBeTruthy();
    expect(screen.getByRole('button', { name: '실제 분석 시작' })).toBeDisabled();
    expect(mocks.submit).not.toHaveBeenCalled();
  });

  it('explicitly selects a real Tool and does not claim it executed', async () => {
    renderLive(`/run/${conversationId}`);
    fireEvent.click(await screen.findByRole('button', { name: '실제 Tool 검색·선택' }));
    await waitFor(() => expect(mocks.select).toHaveBeenCalledWith(requestId));
    expect(await screen.findByText(/선택 Tool: 날씨 조회/)).toBeTruthy();
    expect(screen.getByText('자동 선택')).toBeTruthy();
    expect(screen.getByText(/MCP 호출 또는 업무 실행 완료를 의미하지 않습니다/)).toBeTruthy();
    expect(screen.getByText('Execution: 생성되지 않음')).toBeTruthy();
    expect(mocks.select).toHaveBeenCalledTimes(1);
  });

  it('restores a persisted selection after F5 without rerunning Tool selection', async () => {
    mocks.getSelection.mockResolvedValue({
      request: row('BUILDING_PARAMETERS', { intent: 'Find documents' }),
      selection: selection('AUTO_SELECT'),
    });
    renderLive(`/run/${conversationId}`);
    expect(await screen.findByText(/선택 Tool: 날씨 조회/)).toBeTruthy();
    expect(mocks.select).not.toHaveBeenCalled();
    expect(mocks.analyze).not.toHaveBeenCalled();
  });

  it('does not auto-retry persisted ANALYZING after reload', async () => {
    mocks.latest.mockResolvedValue(row('ANALYZING'));
    mocks.getSelection.mockResolvedValue({
      request: row('ANALYZING'), selection: null,
    });
    mocks.progress.mockResolvedValue(stage('ANALYZING'));
    renderLive(`/run/${conversationId}`);
    expect(await screen.findByText(/분석이 진행 중이거나 중단됐을 수 있습니다/)).toBeTruthy();
    expect(mocks.analyze).not.toHaveBeenCalled();
  });

  it('runs explicit Parameter Builder without starting Plan or Execution', async () => {
    mocks.latest.mockResolvedValue(row('BUILDING_PARAMETERS'));
    mocks.getSelection.mockResolvedValue({ request: row('BUILDING_PARAMETERS'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('BUILDING_PARAMETERS'));
    renderLive('/run/' + conversationId);
    fireEvent.click(await screen.findByRole('button', { name: '입력 파라미터 구성' }));
    await waitFor(() => expect(mocks.build).toHaveBeenCalledWith(requestId));
    expect(await screen.findByText('필수 입력 완료')).toBeTruthy();
    expect(mocks.generate).not.toHaveBeenCalled();
    expect(mocks.validate).not.toHaveBeenCalled();
    expect(screen.getByText('Execution: 생성되지 않음')).toBeTruthy();
  });

  it('generates and validates a persisted single-Tool plan explicitly', async () => {
    mocks.latest.mockResolvedValue(row('PLANNING'));
    mocks.getSelection.mockResolvedValue({ request: row('PLANNING'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('PLANNING'));
    renderLive('/run/' + conversationId);
    fireEvent.click(await screen.findByRole('button', { name: '실행계획 생성' }));
    await waitFor(() => expect(mocks.generate).toHaveBeenCalledWith(requestId));
    expect(await screen.findByText('목적: 문서 검색')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '실행계획 검증' }));
    await waitFor(() => expect(mocks.validate).toHaveBeenCalledWith(requestId));
    expect(await screen.findByText(/계획 검증이 완료됐습니다/)).toBeTruthy();
    expect(mocks.select).not.toHaveBeenCalled();
    expect(screen.getByText('Execution: 생성되지 않음')).toBeTruthy();
  });

  it('submits plan confirmation without auto-creating an Execution', async () => {
    mocks.latest.mockResolvedValue(row('WAITING_CONFIRMATION'));
    mocks.getSelection.mockResolvedValue({ request: row('WAITING_CONFIRMATION'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('WAITING_CONFIRMATION', {
      open_clarification: {
        id: 'clarify-1', request_type: 'PLAN_CONFIRMATION',
        prompt_text: '생성된 실행 계획을 진행하시겠습니까?',
        fields: [{ name: 'confirmed', value_type: 'boolean', description: null, required: true }],
        requested_at: '2026-10-23T10:04:00Z',
      },
    }));
    renderLive('/run/' + conversationId);
    fireEvent.click(await screen.findByRole('button', { name: '확인하고 계속' }));
    await waitFor(() => expect(mocks.respond).toHaveBeenCalledWith(
      requestId, 'clarify-1', { confirmed: true },
    ));
    expect(mocks.validate).not.toHaveBeenCalled();
  });

  it('collects typed required fields and never starts an Execution', async () => {
    mocks.latest.mockResolvedValue(row('WAITING_INPUT'));
    mocks.getSelection.mockResolvedValue({ request: row('WAITING_INPUT'), selection: null });
    mocks.progress.mockResolvedValue(stage('WAITING_INPUT', {
      open_clarification: {
        id: 'clarify-2', request_type: 'MISSING_PARAMETER',
        prompt_text: '추가 입력이 필요합니다: city, amount',
        fields: [
          { name: 'city', value_type: 'string', description: null, required: true },
          { name: 'amount', value_type: 'integer', description: null, required: true },
        ],
        requested_at: '2026-10-23T10:04:00Z',
      },
    }));
    renderLive('/run/' + conversationId);
    fireEvent.change(await screen.findByLabelText('city (필수)'), { target: { value: '서울' } });
    fireEvent.change(screen.getByLabelText('amount (필수)'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: '입력 내용 제출' }));
    await waitFor(() => expect(mocks.respond).toHaveBeenCalledWith(
      requestId, 'clarify-2', { city: '서울', amount: 3 },
    ));
    expect(mocks.build).not.toHaveBeenCalled();
  });



  it('restores the saved plan and validation after reload without reprocessing', async () => {
    mocks.latest.mockResolvedValue(row('READY'));
    mocks.getSelection.mockResolvedValue({ request: row('READY'), selection: selection() });
    mocks.progress.mockResolvedValue(stage('READY', {
      plan: { id: 'saved-plan', plan_hash: 'b'.repeat(64), goal: '저장된 계획', step_count: 1, step_names: ['도구 조회'], created_at: '2026-10-23T10:02:00Z' },
      validation: { id: 'saved-validation', decision: 'READY', confirmation_required: false, error_count: 0, warning_count: 0, created_at: '2026-10-23T10:03:00Z' },
    }));
    renderLive('/run/' + conversationId);
    expect(await screen.findByText('목적: 저장된 계획')).toBeTruthy();
    expect(mocks.generate).not.toHaveBeenCalled();
    expect(mocks.validate).not.toHaveBeenCalled();
    expect(mocks.select).not.toHaveBeenCalled();
  });

});
