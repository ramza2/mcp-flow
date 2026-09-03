import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import { mockJobs } from '../../data/mock';

export default function Jobs() {
  const navigate = useNavigate();

  const columns: Column<typeof mockJobs[0]>[] = [
    { key: 'id', label: 'Job ID', render: r => <span className="font-mono text-xs text-slate-500">{r.id}</span> },
    { key: 'type', label: 'Type', render: r => <span className="font-medium text-slate-800">{r.type}</span> },
    { key: 'resource', label: 'Resource', render: r => <span className="text-sm text-slate-600">{r.resource}</span> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'progress', label: 'Progress', render: r => <span className="text-xs text-slate-400">{r.progress ?? '–'}</span>, align: 'center' },
    { key: 'started', label: '시작', render: r => <span className="text-xs text-slate-500">{r.started}</span> },
    { key: 'duration', label: 'Duration', render: r => <span className="font-mono text-xs text-slate-500">{r.duration}</span> },
    { key: 'error', label: '오류', render: r => r.error ? <span className="text-xs text-red-600 truncate max-w-48 block">{r.error}</span> : <span className="text-slate-300">–</span> },
  ];

  return (
    <div>
      <PageHeader title="Jobs" description="시스템 비동기 작업 상태를 모니터링합니다." />
      <div className="p-6">
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockJobs}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/admin/jobs/${r.id}`)}
          />
        </div>
        <p className="text-xs text-slate-400 mt-2">Progress를 계산할 수 없는 Job에는 비율을 표시하지 않습니다.</p>
      </div>
    </div>
  );
}
