import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import AgentEdit from '@/screens/build/AgentEdit';
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
  createAgentVersion,
  deleteAgent,
  getAgent,
  getAgentVersion,
  listAgentToolGrants,
  listAgentVersions,
  putAgentToolGrants,
  updateAgent,
  validateAgentVersion,
} from '@/api/agents';
import { listLLMProfiles } from '@/api/modelProfiles';
import { listMCPServers, listMCPTools } from '@/api/mcp';

const llmProfile = {
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
};

const savedAgent = {
  id: 'agt-saved',
  code: 'saved-agent',
  name: 'Saved Agent',
  description: null,
  status: 'DRAFT' as const,
  current_version_id: null,
  owner_id: 'user-1',
  visibility: 'PRIVATE' as const,
  created_at: '2026-10-23T00:00:00Z',
  created_by: 'user-1',
  updated_at: '2026-10-23T00:00:00Z',
  updated_by: 'user-1',
  lock_version: 1,
};

const savedVersion = {
  id: 'ver-1',
  agent_id: 'agt-saved',
  version_no: 1,
  status: 'DRAFT' as const,
  system_instruction: 'plan safely',
  llm_profile_id: 'llm-1',
  request_schema_version: '1.0',
  plan_schema_version: '1.0',
  selection_settings: {
    auto_select_threshold: 0.82,
    confirmation_threshold: 0.6,
    max_candidates: 5,
  },
  planning_settings: {},
  response_settings: {},
  validation_status: 'VALID' as const,
  validation_report: { valid: true, errors: [] },
  content_hash: 'a'.repeat(64),
  change_summary: 'initial',
  published_at: null,
  published_by: null,
  deprecated_at: null,
  deprecated_by: null,
  created_at: '2026-10-23T00:00:00Z',
  created_by: null,
};

async function mockCatalog() {
  vi.mocked(listLLMProfiles).mockResolvedValue({
    items: [llmProfile],
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
}

function fillNewAgentForm(name: string, instructions: string) {
  const textboxes = screen.getAllByRole('textbox');
  const nameInput = textboxes.find(el => el.tagName === 'INPUT') as HTMLInputElement;
  fireEvent.change(nameInput, { target: { value: name } });
  const textareas = textboxes.filter(el => el.tagName === 'TEXTAREA');
  // Description then Instructions — last textarea is instructions.
  fireEvent.change(textareas[textareas.length - 1], { target: { value: instructions } });
}

describe('AgentEdit persist / validate safety', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await mockCatalog();
  });

  it('does not validate a previous version while the form is dirty', async () => {
    vi.mocked(getAgent).mockResolvedValue(savedAgent);
    vi.mocked(listAgentVersions).mockResolvedValue({
      items: [savedVersion],
      page: 1,
      page_size: 50,
      total: 1,
      has_next: false,
    });
    vi.mocked(getAgentVersion).mockResolvedValue(savedVersion);
    vi.mocked(listAgentToolGrants).mockResolvedValue({ items: [] });

    renderWithRouter(<AgentEdit />, {
      path: '/agents/:agentId/versions/:versionId/edit',
      route: '/agents/agt-saved/versions/ver-1/edit',
    });

    await waitFor(() => {
      expect(screen.getByDisplayValue('Saved Agent')).toBeInTheDocument();
      expect(screen.getByDisplayValue('plan safely')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByDisplayValue('plan safely'), {
      target: { value: 'changed instructions' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }));

    await waitFor(() => {
      expect(screen.getByText(/저장되지 않은 변경사항이 있습니다/)).toBeInTheDocument();
    });
    expect(validateAgentVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/검증 결과: VALID/)).not.toBeInTheDocument();
  });

  it('reuses in-memory Agent after version create failure (no duplicate create)', async () => {
    const created = {
      ...savedAgent,
      id: 'agt-partial',
      name: 'Partial Agent',
      code: 'partial-agent',
    };
    vi.mocked(createAgent).mockResolvedValue(created);
    vi.mocked(updateAgent).mockResolvedValue({ ...created, lock_version: 2 });
    vi.mocked(createAgentVersion)
      .mockRejectedValueOnce(new Error('version failed'))
      .mockResolvedValueOnce({ ...savedVersion, id: 'ver-2', agent_id: 'agt-partial' });
    vi.mocked(putAgentToolGrants).mockResolvedValue({ items: [] });

    renderWithRouter(<AgentEdit />, {
      path: '/agents/:agentId/versions/:versionId/edit',
      route: '/agents/new/versions/new/edit',
    });

    await waitFor(() => {
      expect(screen.getByText(/New Agent · Draft/i)).toBeInTheDocument();
    });

    fillNewAgentForm('Partial Agent', 'instructions here');
    fireEvent.click(screen.getAllByRole('button', { name: 'Draft 저장' })[0]);

    await waitFor(() => {
      expect(createAgent).toHaveBeenCalledTimes(1);
      expect(createAgentVersion).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/version failed/i)).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Draft 저장' })[0]);

    await waitFor(() => {
      expect(createAgent).toHaveBeenCalledTimes(1);
      expect(updateAgent).toHaveBeenCalled();
      expect(createAgentVersion).toHaveBeenCalledTimes(2);
      expect(putAgentToolGrants).toHaveBeenCalled();
    });
  });

  it('soft-deletes partially created Agent when canceling local New Agent', async () => {
    const created = {
      ...savedAgent,
      id: 'agt-orphan',
      name: 'Orphan Agent',
      code: 'orphan-agent',
    };
    vi.mocked(createAgent).mockResolvedValue(created);
    vi.mocked(createAgentVersion).mockRejectedValue(new Error('version failed'));
    vi.mocked(deleteAgent).mockResolvedValue(undefined);

    const { router } = renderWithRouter(<AgentEdit />, {
      path: '/agents/:agentId/versions/:versionId/edit',
      route: '/agents/new/versions/new/edit',
    });

    await waitFor(() => {
      expect(screen.getByText(/New Agent · Draft/i)).toBeInTheDocument();
    });

    fillNewAgentForm('Orphan Agent', 'orphan instructions');
    fireEvent.click(screen.getAllByRole('button', { name: 'Draft 저장' })[0]);

    await waitFor(() => {
      expect(createAgent).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/version failed/i)).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByRole('button', { name: '취소' })[0]);

    await waitFor(() => {
      expect(deleteAgent).toHaveBeenCalledWith('agt-orphan');
      expect(router.state.location.pathname).toBe('/agents');
    });
  });
});
