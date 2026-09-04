import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge, { RiskBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import { mockApprovals } from '../../data/mock';
import { APPROVAL_STATUSES } from '../../domain';

export default function Approvals() {
  const navigate = useNavigate();

  const columns: Column<typeof mockApprovals[0]>[] = [
    { key: 'purpose', label: '목적', render: r => <span className="font-medium text-slate-800">{r.purpose}</span> },
    { key: 'requester', label: '요청자', render: r => <span className="font-mono text-xs text-slate-600">{r.requester}</span> },
    { key: 'agent', label: 'Agent', render: r => <span className="text-sm text-slate-600">{r.agent}</span> },
    { key: 'tool', label: 'Tool', render: r => <span className="font-mono text-xs text-slate-600">{r.tool}</span> },
    { key: 'riskClass', label: 'Risk', render: r => <RiskBadge risk={r.riskClass} /> },
    { key: 'requestedAt', label: '요청 시각', render: r => <span className="text-xs text-slate-500">{r.requestedAt}</span> },
    { key: 'expiresAt', label: '만료 시각', render: r => <span className="text-xs text-slate-500">{r.expiresAt}</span> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
  ];

  return (
    <div>
      <PageHeader title="Approvals" description="승인 요청을 검토하고 처리합니다." />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="목적, 요청자 검색..."
          filters={[
            { key: 'status', label: '상태', options: APPROVAL_STATUSES.map(v => ({
              value: v,
              label: v.replace(/_/g, ' '),
            })) },
            { key: 'risk', label: 'Risk Class', options: [
              { value: 'NON_IDEMPOTENT_WRITE', label: 'Non-Idempotent Write' },
              { value: 'DESTRUCTIVE', label: 'Destructive' },
            ]},
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockApprovals}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/approvals/${r.id}`)}
          />
        </div>
      </div>
    </div>
  );
}
