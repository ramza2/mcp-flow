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
  createAgentVersion: vi.fn(),
  putAgentToolGrants: vi.fn(),
  updateAgent: vi.fn(),
  validateAgentVersion: vi.fn(),
  publishAgentVersion: vi.fn(),
  activateAgent: vi.fn(),
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
  getAgent,
  listAgentToolGrants,
  listAgentVersions,
} from '@/api/agents';
import { listLLMProfiles } from '@/api/modelProfiles';
import { listMCPServers, listMCPTools } from '@/api/mcp';

describe('New Agent / New Workflow draft regression', () => {
  beforeEach(() => {
    vi.mocked(getAgent).mockResolvedValue({
      id: 'agt-newblank01',
      code: 'new-agent',
      name: 'New Agent',
      description: null,
      status: 'DRAFT',
      current_version_id: null,
      owner_id: 'user-1',
      visibility: 'PRIVATE',
      created_at: '2026-10-23T00:00:00Z',
      created_by: 'user-1',
      updated_at: '2026-10-23T00:00:00Z',
      updated_by: 'user-1',
      lock_version: 1,
    });
    vi.mocked(listAgentVersions).mockResolvedValue({
      items: [],
      page: 1,
      page_size: 50,
      total: 0,
      has_next: false,
    });
    vi.mocked(listAgentToolGrants).mockResolvedValue({ items: [] });
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

  it('API-backed new Agent draft does not inherit mock General Work Assistant content', async () => {
    renderWithRouter(<AgentEdit />, {
      path: '/agents/:agentId/versions/:versionId/edit',
      route: '/agents/agt-newblank01/versions/new/edit',
    });

    await waitFor(() => {
      expect(screen.getByText(/New Agent · Draft/i)).toBeInTheDocument();
    });
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
