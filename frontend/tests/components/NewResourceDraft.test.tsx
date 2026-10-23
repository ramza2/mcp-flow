import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import AgentEdit from '@/screens/build/AgentEdit';
import WorkflowDesigner from '@/screens/build/WorkflowDesigner';
import { renderWithRouter } from '../test-utils';

vi.mock('@/api/agents', () => ({
  getAgent: vi.fn(),
  listAgentVersions: vi.fn(),
  getAgentVersion: vi.fn(),
  listAgentToolGrants: vi.fn(),
  createAgent: vi.fn(),
  createAgentVersion: vi.fn(),
  putAgentToolGrants: vi.fn(),
  updateAgent: vi.fn(),
  validateAgentVersion: vi.fn(),
  publishAgentVersion: vi.fn(),
  activateAgent: vi.fn(),
  deleteAgent: vi.fn(),
}));

vi.mock('@/api/modelProfiles', () => ({
  listLLMProfiles: vi.fn(),
}));

vi.mock('@/api/mcp', async () => {
  const actual = await vi.importActual<typeof import('@/api/mcp')>('@/api/mcp');
  return {
    ...actual,
    listMCPTools: vi.fn(),
    listMCPServers: vi.fn(),
  };
});

import {
  createAgent,
  getAgent,
  listAgentToolGrants,
  listAgentVersions,
} from '@/api/agents';
import { listLLMProfiles } from '@/api/modelProfiles';
import { listMCPServers, listMCPTools } from '@/api/mcp';

describe('New Agent / New Workflow draft regression', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listLLMProfiles).mockResolvedValue({
      items: [{
        id: 'llm-1',
        code: 'llm-1',
        name: 'Test LLM',
        provider: 'OPENAI_COMPATIBLE',
        model: 'gpt-test',
        base_url: 'https://llm.test/v1',
        credential_secret_id: null,
        parameters: null,
        request_timeout_ms: null,
        status: null,
        created_at: '2026-10-23T00:00:00Z',
        updated_at: '2026-10-23T00:00:00Z',
        lock_version: 1,
      }],
      page: 1,
      page_size: 100,
      total: 1,
      has_next: false,
    });
    vi.mocked(listMCPTools).mockResolvedValue({
      items: [],
      page: 1,
      page_size: 100,
      total: 0,
      has_next: false,
    });
    vi.mocked(listMCPServers).mockResolvedValue({
      items: [],
      page: 1,
      page_size: 100,
      total: 0,
      has_next: false,
    });
  });

  it('local /agents/new draft does not call createAgent or inherit mock content', async () => {
    renderWithRouter(<AgentEdit />, {
      path: '/agents/:agentId/versions/:versionId/edit',
      route: '/agents/new/versions/new/edit',
    });

    await waitFor(() => {
      expect(screen.getByText(/New Agent · Draft/i)).toBeInTheDocument();
    });
    expect(createAgent).not.toHaveBeenCalled();
    expect(getAgent).not.toHaveBeenCalled();
    expect(listAgentVersions).not.toHaveBeenCalled();
    expect(listAgentToolGrants).not.toHaveBeenCalled();
    expect(screen.getByText(/아직 DB에 저장되지 않았습니다/i)).toBeInTheDocument();
    expect(screen.queryByDisplayValue('General Work Assistant')).not.toBeInTheDocument();
    expect(screen.queryByText(/당신은 MCPFlow 업무 자동화 Agent/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/agt-001/i)).not.toBeInTheDocument();
  });

  it('unknown wf-* starts with empty/minimal plan and does not clone Weekly Report tools', () => {
    renderWithRouter(<WorkflowDesigner />, {
      path: '/workflows/:workflowId/versions/:versionId/edit',
      route: '/workflows/wf-newblank01/versions/v1/edit',
    });

    expect(screen.getByText(/New Workflow · Draft v1/i)).toBeInTheDocument();
    expect(screen.getByText(/empty plan/i)).toBeInTheDocument();
    expect(screen.queryByText('Weekly Report Workflow')).not.toBeInTheDocument();
    expect(screen.queryByText('Get Data')).not.toBeInTheDocument();
    expect(screen.getByText('Tool 1')).toBeInTheDocument();
  });
});
