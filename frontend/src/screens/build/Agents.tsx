import { useNavigate } from 'react-router';
import { Plus, Bot, Eye } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import { VersionBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import { mockAgents } from '../../data/mock';

export default function Agents() {
  const navigate = useNavigate();

  const columns: Column<typeof mockAgents[0]>[] = [
    { key: 'name', label: 'Name', render: r => (
      <div className="flex items-center gap-2">
        <span className="w-7 h-7 rounded-md bg-indigo-50 text-indigo-600 flex items-center justify-center shrink-0"><Bot size={14} /></span>
        <span className="font-medium text-slate-800">{r.name}</span>
      </div>
    )},
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'publishedVersion', label: 'Published', render: r => r.publishedVersion
      ? <VersionBadge version={r.publishedVersion} status="PUBLISHED" />
      : <span className="text-xs text-slate-400">–</span> },
    { key: 'allowedTools', label: 'Tools', render: r => <span className="text-sm text-slate-600">{r.allowedTools}</span>, align: 'center' },
    { key: 'modelProfile', label: 'Model Profile', render: r => <span className="text-xs text-slate-600">{r.modelProfile}</span> },
    { key: 'owner', label: 'Owner', render: r => <span className="text-xs text-slate-500">{r.owner}</span> },
    { key: 'updatedAt', label: 'Updated', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
    { key: 'actions', label: '', render: r => (
      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
        <button onClick={() => navigate(`/agents/${r.id}`)} className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50" title="상세"><Eye size={13} /></button>
      </div>
    )},
  ];

  return (
    <div>
      <PageHeader
        title="Agents"
        description="자연어 요청을 처리하는 AI Agent와 버전을 관리합니다."
        actions={<Button icon={<Plus size={14} />} onClick={() => navigate('/agents/agt-001/versions/new/edit')}>New Agent</Button>}
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search searchPlaceholder="Agent 이름 검색..."
          filters={[
            { key: 'status', label: '상태', options: [{ value: 'ACTIVE', label: 'Active' }, { value: 'INACTIVE', label: 'Inactive' }] },
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockAgents}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/agents/${r.id}`)}
            emptyMessage="등록된 Agent가 없습니다."
          />
        </div>
      </div>
    </div>
  );
}
