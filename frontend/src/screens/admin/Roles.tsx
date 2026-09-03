import { useState } from 'react';
import { Check } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';

const ROLES = ['Super Admin', 'Admin', 'Operator', 'Approver', 'User', 'Viewer'];

const PERMISSION_GROUPS = [
  {
    group: 'MCP',
    permissions: ['mcp.server.view', 'mcp.server.manage', 'mcp.tool.view', 'mcp.tool.manage'],
  },
  {
    group: 'Agent',
    permissions: ['agent.view', 'agent.run', 'agent.manage', 'agent.publish'],
  },
  {
    group: 'Workflow',
    permissions: ['workflow.view', 'workflow.run', 'workflow.manage', 'workflow.publish'],
  },
  {
    group: 'Execution',
    permissions: ['execution.view', 'execution.cancel', 'execution.view_all'],
  },
  {
    group: 'Approval',
    permissions: ['approval.view', 'approval.approve', 'approval.manage_policy'],
  },
  {
    group: 'Audit',
    permissions: ['audit.view'],
  },
  {
    group: 'System',
    permissions: ['system.settings.view', 'system.settings.manage', 'system.users.manage'],
  },
];

const DEFAULT_GRANTS: Record<string, string[]> = {
  'Super Admin': PERMISSION_GROUPS.flatMap(g => g.permissions),
  'Admin': PERMISSION_GROUPS.flatMap(g => g.permissions).filter(p => !p.includes('system.settings.manage')),
  'Operator': ['mcp.server.view', 'mcp.tool.view', 'agent.view', 'agent.run', 'workflow.view', 'workflow.run', 'execution.view', 'approval.view'],
  'Approver': ['execution.view', 'approval.view', 'approval.approve'],
  'User': ['agent.view', 'agent.run', 'execution.view'],
  'Viewer': ['mcp.server.view', 'mcp.tool.view', 'agent.view', 'workflow.view', 'execution.view'],
};

export default function Roles() {
  const [selectedRole, setSelectedRole] = useState('Operator');
  const grants = DEFAULT_GRANTS[selectedRole] ?? [];

  return (
    <div>
      <PageHeader title="Roles & Permissions" description="Role별 권한을 구성합니다." />
      <div className="flex h-[calc(100vh-160px)]">
        {/* Role list */}
        <div className="w-48 border-r border-slate-200 bg-white flex-shrink-0 py-3">
          {ROLES.map(role => (
            <button
              key={role}
              onClick={() => setSelectedRole(role)}
              className={`w-full text-left px-4 py-2.5 text-sm transition-colors
                ${selectedRole === role ? 'bg-indigo-50 text-indigo-700 font-medium' : 'text-slate-600 hover:bg-slate-50'}`}
            >
              {role}
            </button>
          ))}
        </div>

        {/* Permission matrix */}
        <div className="flex-1 overflow-auto p-4">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold text-slate-800">{selectedRole} — Permissions</h3>
            <div className="flex gap-2">
              <Button size="sm" variant="outline">전체 선택</Button>
              <Button size="sm" variant="outline">전체 해제</Button>
              <Button size="sm">저장</Button>
            </div>
          </div>
          <div className="space-y-4">
            {PERMISSION_GROUPS.map(group => (
              <div key={group.group} className="bg-white rounded-xl border border-slate-200 overflow-hidden">
                <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-100">
                  <h4 className="text-xs font-semibold text-slate-600 uppercase tracking-wide">{group.group}</h4>
                </div>
                <div className="p-3 grid grid-cols-2 gap-2">
                  {group.permissions.map(perm => {
                    const granted = grants.includes(perm);
                    return (
                      <label key={perm} className={`flex items-center gap-2.5 p-2.5 rounded-lg cursor-pointer transition-colors ${granted ? 'bg-indigo-50' : 'hover:bg-slate-50'}`}>
                        <div className={`w-4 h-4 rounded border-2 flex items-center justify-center shrink-0 ${granted ? 'border-indigo-600 bg-indigo-600' : 'border-slate-300'}`}>
                          {granted && <Check size={9} className="text-white" />}
                        </div>
                        <span className="font-mono text-xs text-slate-700">{perm}</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
