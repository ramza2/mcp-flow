import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import Agents, { NEW_AGENT_PATH } from '@/screens/build/Agents';
import AgentDetail, {
  activateDisabledReason,
  buildAgentSetupSteps,
  nextAgentSetupAction,
  publishDisabledReason,
} from '@/screens/build/AgentDetail';
import { renderWithRouter } from '../test-utils';

vi.mock('@/api/agents', () => ({
  activateAgent: vi.fn(),
  deleteAgent: vi.fn(),
  getAgent: vi.fn(),
  listAgentToolGrants: vi.fn(),
  listAgentVersions: vi.fn(),
  listAgents: vi.fn(),
  publishAgentVersion: vi.fn(),
  validateAgentVersion: vi.fn(),
}));

vi.mock('@/domain', async () => {
  const actual = await vi.importActual<typeof import('@/domain')>('@/domain');
  return {
    ...actual,
    hasPermission: vi.fn(actual.hasPermission),
  };
});

import {
  getAgent,
  listAgentToolGrants,
  listAgentVersions,
  listAgents,
} from '@/api/agents';
import { hasPermission } from '@/domain';

const mockedHasPermission = vi.mocked(hasPermission);

describe('Agent beginner UX helpers', () => {
  it('builds create→validate→publish→activate→run from existing fields only', () => {
    const steps = buildAgentSetupSteps(
      { status: 'DRAFT', current_version_id: null },
      [{ status: 'DRAFT', validation_status: 'INVALID' }],
    );
    expect(steps.map(s => [s.id, s.label, s.done])).toEqual([
      ['create', '생성', true],
      ['validate', '검증', false],
      ['publish', '게시', false],
      ['activate', '활성화', false],
      ['run', '실행 가능', false],
    ]);
    expect(nextAgentSetupAction(steps)?.stepId).toBe('validate');
  });

  it('marks ready when ACTIVE with published current version', () => {
    const steps = buildAgentSetupSteps(
      { status: 'ACTIVE', current_version_id: 'ver-1' },
      [{ status: 'PUBLISHED', validation_status: 'VALID' }],
    );
    expect(steps.every(s => s.done)).toBe(true);
    expect(steps.find(s => s.id === 'run')?.label).toBe('실행 가능');
    expect(nextAgentSetupAction(steps)).toBeNull();
  });

  it('explains publish and activate disables in Korean without new Domain states', () => {
    expect(publishDisabledReason('PUBLISHED')).toMatch(/초안/);
    expect(activateDisabledReason({ status: 'DRAFT', current_version_id: null }))
      .toMatch(/게시된 버전/);
    expect(activateDisabledReason({ status: 'ACTIVE', current_version_id: 'v1' }))
      .toMatch(/이미 활성화/);
  });
});

describe('Agents empty CTA by permission', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAgents).mockResolvedValue({
      items: [],
      page: 1,
      page_size: 20,
      total: 0,
      has_next: false,
    });
  });

  it('shows Empty 새 에이전트 CTA when agent.edit is allowed', async () => {
    mockedHasPermission.mockImplementation(permission =>
      permission === 'agent.edit' || permission === 'agent.view',
    );
    const { router } = renderWithRouter(<Agents />, { path: '/agents', route: '/agents' });

    expect(await screen.findByText('등록된 에이전트가 없습니다')).toBeInTheDocument();
    const buttons = screen.getAllByRole('button', { name: '새 에이전트' });
    expect(buttons.length).toBeGreaterThanOrEqual(2);

    fireEvent.click(buttons[buttons.length - 1]!);
    expect(router.state.location.pathname).toBe(NEW_AGENT_PATH);
  });

  it('hides Empty CTA when agent.edit is denied', async () => {
    mockedHasPermission.mockImplementation(permission => permission === 'agent.view');
    renderWithRouter(<Agents />, { path: '/agents', route: '/agents' });

    expect(await screen.findByText('등록된 에이전트가 없습니다')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '새 에이전트' })).not.toBeInTheDocument();
    expect(screen.getByText(/만들 권한이 없습니다/)).toBeInTheDocument();
  });
});

describe('AgentDetail beginner progress and CTAs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listAgentToolGrants).mockResolvedValue({ items: [] });
  });

  it('shows publish/activate disabled reasons and next step for draft without publish', async () => {
    vi.mocked(getAgent).mockResolvedValue({
      id: 'agent-1', code: 'agent-test', name: '초안 도우미',
      description: null, status: 'DRAFT', visibility: 'PRIVATE',
      current_version_id: null, owner_id: 'user-1',
      created_at: '2026-11-09T00:00:00Z', created_by: 'user-1',
      updated_at: '2026-11-09T00:00:00Z', updated_by: 'user-1',
      lock_version: 1,
    });
    vi.mocked(listAgentVersions).mockResolvedValue({
      items: [{
        id: 'version-1', agent_id: 'agent-1', version_no: 1,
        status: 'DRAFT', system_instruction: '조회하세요.',
        llm_profile_id: 'llm-1', request_schema_version: '1.0',
        plan_schema_version: '1.0', selection_settings: {},
        planning_settings: {}, response_settings: {},
        validation_status: 'INVALID', validation_report: { valid: false, errors: [] },
        content_hash: 'a'.repeat(64), change_summary: null,
        published_at: null, published_by: null,
        deprecated_at: null, deprecated_by: null,
        created_at: '2026-11-09T00:00:00Z', created_by: 'user-1',
      }],
      page: 1, page_size: 50, total: 1, has_next: false,
    });

    renderWithRouter(<AgentDetail />, {
      path: '/agents/:agentId', route: '/agents/agent-1',
    });

    expect(await screen.findByTestId('agent-setup-progress')).toBeInTheDocument();
    expect(screen.getByTestId('agent-setup-step-create')).toHaveAttribute('data-done', 'true');
    expect(screen.getByTestId('agent-setup-step-validate')).toHaveAttribute('data-done', 'false');
    expect(screen.getByTestId('agent-setup-next')).toHaveTextContent(/검증/);

    const reasons = screen.getByTestId('agent-action-disabled-reasons');
    expect(within(reasons).getByText(/활성화 불가/)).toHaveTextContent(/게시된 버전/);
    expect(screen.queryByRole('button', { name: '에이전트 실행으로 이동' })).not.toBeInTheDocument();
  });

  it('shows /run CTA when ACTIVE with published version', async () => {
    vi.mocked(getAgent).mockResolvedValue({
      id: 'agent-1', code: 'agent-test', name: '준비된 도우미',
      description: null, status: 'ACTIVE', visibility: 'PRIVATE',
      current_version_id: 'version-1', owner_id: 'user-1',
      created_at: '2026-11-09T00:00:00Z', created_by: 'user-1',
      updated_at: '2026-11-09T00:00:00Z', updated_by: 'user-1',
      lock_version: 2,
    });
    vi.mocked(listAgentVersions).mockResolvedValue({
      items: [{
        id: 'version-1', agent_id: 'agent-1', version_no: 1,
        status: 'PUBLISHED', system_instruction: '조회하세요.',
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

    const { router } = renderWithRouter(<AgentDetail />, {
      path: '/agents/:agentId', route: '/agents/agent-1',
    });

    expect(await screen.findByRole('button', { name: '에이전트 실행으로 이동' })).toBeInTheDocument();
    expect(screen.getByTestId('agent-setup-next')).toHaveTextContent(/준비 완료/);
    expect(screen.getByTestId('agent-action-disabled-reasons')).toHaveTextContent(/게시 불가/);
    expect(screen.getByTestId('agent-action-disabled-reasons')).toHaveTextContent(/이미 활성화/);

    fireEvent.click(screen.getByRole('button', { name: '에이전트 실행으로 이동' }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/run');
      expect(router.state.location.search).toBe('?agentId=agent-1');
    });
  });
});

describe('Agents New Agent still works with Empty CTA', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedHasPermission.mockReturnValue(true);
    vi.mocked(listAgents).mockResolvedValue({
      items: [],
      page: 1,
      page_size: 20,
      total: 0,
      has_next: false,
    });
  });

  it('header 새 에이전트 still navigates locally without POST', async () => {
    const { router } = renderWithRouter(<Agents />, {
      path: '/agents',
      route: '/agents',
    });

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: '새 에이전트' }).length).toBeGreaterThanOrEqual(1);
    });

    fireEvent.click(screen.getAllByRole('button', { name: '새 에이전트' })[0]!);
    expect(router.state.location.pathname).toBe(NEW_AGENT_PATH);
  });
});
