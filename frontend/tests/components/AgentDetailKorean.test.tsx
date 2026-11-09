import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import AgentDetail from '@/screens/build/AgentDetail';
import { renderWithRouter } from '../test-utils';

vi.mock('@/api/agents', () => ({
  activateAgent: vi.fn(),
  deleteAgent: vi.fn(),
  getAgent: vi.fn(),
  listAgentToolGrants: vi.fn(),
  listAgentVersions: vi.fn(),
  publishAgentVersion: vi.fn(),
  validateAgentVersion: vi.fn(),
}));

import { getAgent, listAgentToolGrants, listAgentVersions } from '@/api/agents';

describe('Agent detail Korean presentation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAgent).mockResolvedValue({
      id: 'agent-1', code: 'agent-test', name: '자료 조회 도우미',
      description: '조회만 합니다', status: 'ACTIVE', visibility: 'RESTRICTED',
      current_version_id: 'version-1', owner_id: 'user-1',
      created_at: '2026-11-09T00:00:00Z', created_by: 'user-1',
      updated_at: '2026-11-09T00:00:00Z', updated_by: 'user-1',
      lock_version: 1,
    });
    vi.mocked(listAgentVersions).mockResolvedValue({
      items: [{
        id: 'version-1', agent_id: 'agent-1', version_no: 1,
        status: 'PUBLISHED', system_instruction: '허용된 도구로 조회하세요.',
        llm_profile_id: 'llm-1', request_schema_version: '1.0',
        plan_schema_version: '1.0', selection_settings: {},
        planning_settings: {}, response_settings: {},
        validation_status: 'VALID', validation_report: { valid: true, errors: [] },
        content_hash: 'a'.repeat(64), change_summary: null,
        published_at: '2026-11-09T00:00:00Z', published_by: 'user-1',
        deprecated_at: null, deprecated_by: null,
        created_at: '2026-11-09T00:00:00Z', created_by: 'user-1',
      }],
      page: 1, page_size: 50, total: 1, has_next: false,
    });
    vi.mocked(listAgentToolGrants).mockResolvedValue({ items: [] });
  });

  it('renders Korean tabs, labels and instructions without changing the canonical version state', async () => {
    renderWithRouter(<AgentDetail />, {
      path: '/agents/:agentId', route: '/agents/agent-1',
    });

    expect(await screen.findByText('자료 조회 도우미')).toBeInTheDocument();
    expect(screen.getByText('제한 공개')).toHaveAttribute('title', 'RESTRICTED');
    expect(screen.getByText('활성')).toBeInTheDocument();
    fireEvent.click(screen.getByText('버전 이력'));
    expect(screen.getByText('게시됨')).toBeInTheDocument();
    fireEvent.click(screen.getByText('업무 지시문'));
    expect(screen.getByText('허용된 도구로 조회하세요.')).toBeInTheDocument();
  });
});
