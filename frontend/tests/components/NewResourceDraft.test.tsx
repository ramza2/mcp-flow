import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import AgentEdit from '@/screens/build/AgentEdit';
import WorkflowDesigner from '@/screens/build/WorkflowDesigner';
import { renderWithRouter } from '../test-utils';

describe('New Agent / New Workflow draft regression', () => {
  it('unknown agt-* starts as blank DRAFT and does not inherit General Work Assistant', () => {
    renderWithRouter(<AgentEdit />, {
      path: '/agents/:agentId/versions/:versionId/edit',
      route: '/agents/agt-newblank01/versions/v1/edit',
    });

    expect(screen.getByText(/New Agent · Draft v1/i)).toBeInTheDocument();
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
