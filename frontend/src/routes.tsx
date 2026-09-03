import { createBrowserRouter } from 'react-router';
import AppShell from './components/layout/AppShell';

// Auth
import Login from './screens/Login';

// Work
import Dashboard from './screens/Dashboard';
import AgentRun from './screens/work/AgentRun';
import Executions from './screens/work/Executions';
import ExecutionDetail from './screens/work/ExecutionDetail';
import Approvals from './screens/work/Approvals';
import ApprovalDetail from './screens/work/ApprovalDetail';
import Schedules from './screens/work/Schedules';
import ScheduleEdit from './screens/work/ScheduleEdit';

// Build
import Agents from './screens/build/Agents';
import AgentDetail from './screens/build/AgentDetail';
import AgentEdit from './screens/build/AgentEdit';
import Workflows from './screens/build/Workflows';
import WorkflowDetail from './screens/build/WorkflowDetail';
import WorkflowDesigner from './screens/build/WorkflowDesigner';

// MCP
import MCPServers from './screens/mcp/MCPServers';
import MCPServerNew from './screens/mcp/MCPServerNew';
import MCPServerDetail from './screens/mcp/MCPServerDetail';
import MCPTools from './screens/mcp/MCPTools';
import MCPToolDetail from './screens/mcp/MCPToolDetail';
import ExternalDiscovery from './screens/mcp/ExternalDiscovery';
import ToolFactory from './screens/mcp/ToolFactory';
import ToolFactoryNew from './screens/mcp/ToolFactoryNew';
import FactoryBuildDetail from './screens/mcp/FactoryBuildDetail';

// Admin
import Users from './screens/admin/Users';
import UserDetail from './screens/admin/UserDetail';
import Roles from './screens/admin/Roles';
import ApprovalPolicies from './screens/admin/ApprovalPolicies';
import ModelProfiles from './screens/admin/ModelProfiles';
import ModelProfileDetail from './screens/admin/ModelProfileDetail';
import AuditLogs from './screens/admin/AuditLogs';
import Jobs from './screens/admin/Jobs';
import JobDetail from './screens/admin/JobDetail';
import SystemSettings from './screens/admin/SystemSettings';

export const router = createBrowserRouter([
  {
    path: '/login',
    Component: Login,
  },
  {
    path: '/',
    Component: AppShell,
    children: [
      { index: true, Component: Dashboard },

      // Work
      { path: 'run', Component: AgentRun },
      { path: 'run/:conversationId', Component: AgentRun },
      { path: 'executions', Component: Executions },
      { path: 'executions/:executionId', Component: ExecutionDetail },
      { path: 'approvals', Component: Approvals },
      { path: 'approvals/:approvalId', Component: ApprovalDetail },
      { path: 'schedules', Component: Schedules },
      { path: 'schedules/new', Component: ScheduleEdit },
      { path: 'schedules/:id/edit', Component: ScheduleEdit },

      // Build
      { path: 'agents', Component: Agents },
      { path: 'agents/:agentId', Component: AgentDetail },
      { path: 'agents/:agentId/versions/:versionId/edit', Component: AgentEdit },
      { path: 'workflows', Component: Workflows },
      { path: 'workflows/:workflowId', Component: WorkflowDetail },
      { path: 'workflows/:workflowId/versions/:versionId/edit', Component: WorkflowDesigner },

      // MCP
      { path: 'mcp/servers', Component: MCPServers },
      { path: 'mcp/servers/new', Component: MCPServerNew },
      { path: 'mcp/servers/:serverId', Component: MCPServerDetail },
      { path: 'mcp/tools', Component: MCPTools },
      { path: 'mcp/tools/:toolId', Component: MCPToolDetail },
      { path: 'mcp/discovery', Component: ExternalDiscovery },
      { path: 'tool-factory', Component: ToolFactory },
      { path: 'tool-factory/new', Component: ToolFactoryNew },
      { path: 'tool-factory/:buildId', Component: FactoryBuildDetail },

      // Admin
      { path: 'admin/users', Component: Users },
      { path: 'admin/users/:userId', Component: UserDetail },
      { path: 'admin/roles', Component: Roles },
      { path: 'admin/approval-policies', Component: ApprovalPolicies },
      { path: 'admin/model-profiles', Component: ModelProfiles },
      { path: 'admin/model-profiles/:profileId', Component: ModelProfileDetail },
      { path: 'admin/audit-logs', Component: AuditLogs },
      { path: 'admin/jobs', Component: Jobs },
      { path: 'admin/jobs/:jobId', Component: JobDetail },
      { path: 'admin/settings', Component: SystemSettings },
    ],
  },
]);
