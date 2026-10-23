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
}));

vi.mock('@/api/agentRun', () => ({
  listRunnableAgents: mocks.list,
  submitAgentRun: mocks.submit,
  analyzeAgentRun: mocks.analyze,
  getConversationLatestAgentRun: mocks.latest,
}));

const conversationId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const requestId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const agentId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';

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

  it('does not auto-retry persisted ANALYZING after reload', async () => {
    mocks.latest.mockResolvedValue(row('ANALYZING'));
    renderLive(`/run/${conversationId}`);
    expect(await screen.findByText(/분석이 진행 중이거나 중단됐을 수 있습니다/)).toBeTruthy();
    expect(mocks.analyze).not.toHaveBeenCalled();
  });
});
