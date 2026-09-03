import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge, { RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import { mockTools } from '../../data/mock';

export default function MCPTools() {
  const navigate = useNavigate();

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
            { key: 'status', label: '상태', options: [{ value: 'ACTIVE', label: 'Active' }, { value: 'INACTIVE', label: 'Inactive' }, { value: 'BLOCKED', label: 'Blocked' }, { value: 'MISSING', label: 'Missing' }] },
            { key: 'risk', label: 'Risk Class', options: [
              { value: 'READ_ONLY', label: 'Read Only' },
              { value: 'NON_IDEMPOTENT_WRITE', label: 'Non-Idempotent Write' },
              { value: 'DESTRUCTIVE', label: 'Destructive' },
            ]},
            { key: 'verification', label: 'Verification', options: [{ value: 'VERIFIED', label: 'Verified' }, { value: 'FAILED', label: 'Failed' }, { value: 'EXPIRED', label: 'Expired' }] },
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
