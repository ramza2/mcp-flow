import { useNavigate } from 'react-router';
import { Plus, GitBranch, Eye } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge, { VersionBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import { mockWorkflows } from '../../data/mock';
import { WORKFLOW_STATUSES } from '../../domain';

export default function Workflows() {
  const navigate = useNavigate();

  /** Mock UX: create Logical Workflow + initial DRAFT Version (Designer falls back if id unknown). */
  const handleNewWorkflow = () => {
    const id = `wf-${Date.now().toString(36)}`;
    navigate(`/workflows/${id}/versions/v1/edit`);
  };

  const columns: Column<typeof mockWorkflows[0]>[] = [
    { key: 'name', label: 'Name', render: r => (
      <div className="flex items-center gap-2">
        <span className="w-7 h-7 rounded-md bg-cyan-50 text-cyan-600 flex items-center justify-center shrink-0"><GitBranch size={14} /></span>
        <span className="font-medium text-slate-800">{r.name}</span>
      </div>
    )},
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'publishedVersion', label: 'Published', render: r => r.publishedVersion
      ? <VersionBadge version={r.publishedVersion} status="PUBLISHED" />
      : <span className="text-xs text-slate-400">–</span> },
    { key: 'steps', label: 'Steps', render: r => <span className="text-sm text-slate-600">{r.steps}</span>, align: 'center' },
    { key: 'owner', label: 'Owner', render: r => <span className="text-xs text-slate-500">{r.owner}</span> },
    { key: 'lastPublished', label: 'Last Published', render: r => <span className="text-xs text-slate-400">{r.lastPublished ?? '–'}</span> },
    { key: 'updatedAt', label: 'Updated', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
    { key: 'actions', label: '', render: r => (
      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
        <button onClick={() => navigate(`/workflows/${r.id}`)} className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50" title="상세"><Eye size={13} /></button>
      </div>
    )},
  ];

  return (
    <div>
      <PageHeader
        title="Workflows"
        description="여러 Tool과 Step을 조합한 실행 흐름을 설계하고 관리합니다."
        actions={<Button icon={<Plus size={14} />} onClick={handleNewWorkflow}>New Workflow</Button>}
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search searchPlaceholder="Workflow 이름 검색..."
          filters={[
            { key: 'status', label: '상태', options: WORKFLOW_STATUSES.map(v => ({
              value: v,
              label: v.charAt(0) + v.slice(1).toLowerCase(),
            })) },
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockWorkflows}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/workflows/${r.id}`)}
            emptyMessage="등록된 Workflow가 없습니다."
          />
        </div>
      </div>
    </div>
  );
}
