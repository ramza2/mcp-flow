import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge, { RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import { mockMCPServers, mockTools } from '../../data/mock';
import {
  MCP_TOOL_STATUSES,
  RISK_CLASSES,
  TOOL_VERIFICATION_STATUSES,
  TOOL_VERSION_VALIDATION_STATUSES,
} from '../../domain';

export default function MCPTools() {
  const navigate = useNavigate();

  const capabilities = [...new Set(mockTools.map(t => t.capability))].sort();

  const columns: Column<typeof mockTools[0]>[] = [
    { key: 'displayName', label: 'Display Name', render: r => <span className="font-medium text-slate-800">{r.displayName}</span> },
    { key: 'sourceName', label: 'Source Name', render: r => <span className="font-mono text-xs text-slate-500">{r.sourceName}</span> },
    { key: 'serverName', label: 'Server', render: r => <span className="text-sm text-slate-600">{r.serverName}</span> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'riskClass', label: 'Risk Class', render: r => <RiskBadge risk={r.riskClass} /> },
    { key: 'currentVersion', label: 'Version', render: r => <span className="font-mono text-xs text-slate-500">{r.currentVersion}</span> },
    { key: 'validation', label: 'Validation', render: r => {
      const styles = { VALID: 'text-green-700 bg-green-50', WARNING: 'text-amber-700 bg-amber-50', INVALID: 'text-red-700 bg-red-50' };
      const s = styles[r.validation as keyof typeof styles] ?? 'text-slate-500 bg-slate-100';
      return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s}`}>{r.validation}</span>;
    }},
    { key: 'verification', label: 'Verification', render: r => <VerificationBadge status={r.verification} /> },
    { key: 'capability', label: 'Capability', render: r => <span className="text-xs font-mono text-slate-500">{r.capability}</span> },
    { key: 'usedBy', label: 'Used By', render: r => <span className="text-sm text-slate-500">{r.usedBy}개</span>, align: 'center' },
    { key: 'updatedAt', label: '수정일', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
  ];

  return (
    <div>
      <PageHeader title="MCP Tools" description="등록된 MCP Tool의 상태와 정책을 관리합니다." />
      <div className="p-6 space-y-4">
        <FilterBar
          search searchPlaceholder="Tool 이름, 서버 검색..."
          filters={[
            { key: 'status', label: 'Tool Status', options: MCP_TOOL_STATUSES.map(v => ({ value: v, label: v })) },
            { key: 'risk', label: 'Risk Class', options: RISK_CLASSES.map(v => ({ value: v, label: v.replace(/_/g, ' ') })) },
            { key: 'validation', label: 'Version Validation', options: TOOL_VERSION_VALIDATION_STATUSES.map(v => ({ value: v, label: v })) },
            { key: 'verification', label: 'Verification', options: TOOL_VERIFICATION_STATUSES.map(v => ({ value: v, label: v })) },
            { key: 'server', label: 'Server', options: mockMCPServers.map(s => ({ value: s.id, label: s.name })) },
            { key: 'capability', label: 'Tag / Capability', options: capabilities.map(c => ({ value: c, label: c })) },
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockTools}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/mcp/tools/${r.id}`)}
          />
        </div>
      </div>
    </div>
  );
}
